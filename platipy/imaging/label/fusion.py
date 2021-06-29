# Copyright 2020 University of New South Wales, University of Sydney, Ingham Institute

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at

#     http://www.apache.org/licenses/LICENSE-2.0

# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import numpy as np
import SimpleITK as sitk

from functools import reduce
from scipy.stats import pearsonr
from skimage.util.shape import view_as_windows
from platipy.imaging.registration.utils import smooth_and_resample


def mutual_information(arr_a, arr_b, bins=64):
    """Computes the (histogram-based) mutual information between two arrays

    Args:
        arr_a (np.ndarray): The first image array values, should be flattened to a 1D array.
        arr_b (np.ndarray): The second image array values, should be flattened to a 1D array.
        bins (np.ndarray | int, optional): Histogram bins. Passed directly to np.histogram2d, so
            any format accepted by this function is okay. Defaults to 64.

    Returns:
        float: The mutual information between the arrays.
    """

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")

        p_ab, _, _ = np.histogram2d(arr_a, arr_b, bins=bins, density=True)

        p_a = p_ab.sum(axis=0)
        p_b = p_ab.sum(axis=1)

        log_p = np.log(p_ab / np.outer(p_a, p_b))

    log_p[~np.isfinite(log_p)] = 0

    mi = (p_ab * log_p).sum()

    return mi


def compute_weight_map(
    target_image,
    moving_image,
    vote_type="unweighted",
    vote_params={
        "sigma": 2.0,
        "epsilon": 1e-5,
        "factor": 1e12,
        "gain": 6,
        "blockSize": 5,
        "normalise": False,
        "patch_window_mm": 25,
        "resampled_voxel_size_mm": 3,
        "correlation_function": lambda x: x + 1,
    },
):
    """
    Computes the weight map
    """

    # Cast to floating point representation, if necessary
    if target_image.GetPixelID() != 6:
        target_image = sitk.Cast(target_image, sitk.sitkFloat32)
    if moving_image.GetPixelID() != 6:
        moving_image = sitk.Cast(moving_image, sitk.sitkFloat32)

    if vote_type.lower() == "patch_correlation":
        # Resample images for 2 reasons:
        # 1. Reduce overall size
        # 2. Take cubic patches
        voxel_size = vote_params["resampled_voxel_size_mm"]
        img_target_res = smooth_and_resample(target_image, isotropic_voxel_size_mm=voxel_size)
        img_moving_res = smooth_and_resample(moving_image, isotropic_voxel_size_mm=voxel_size)

        # Convert to arrats
        arr_target = sitk.GetArrayFromImage(img_target_res)
        arr_moving = sitk.GetArrayFromImage(img_moving_res)
        # The mask will help us deal with zero data at the edges (generated by padding)
        arr_mask = 0 * arr_target + 1

        # Define the patch box in image coordinates
        window_box_mm = vote_params["patch_window_mm"]
        window_box_im = [int(window_box_mm / i) for i in img_target_res.GetSpacing()[::-1]]

        # Pad the arrays
        padder = [((i - 1) // 2, (i) // 2) for i in window_box_im]  # Could pad more at other ends?
        arr_target = np.pad(arr_target, padder)
        arr_moving = np.pad(arr_moving, padder)
        arr_mask = np.pad(arr_mask, padder)

        # Extract the patches as views
        view_target = view_as_windows(arr_target, window_box_im)
        view_moving = view_as_windows(arr_moving, window_box_im)
        view_mask = view_as_windows(arr_mask, window_box_im)

        # Flatten to have a list of patches (that are also flattened)
        new_shape = (np.product(view_target.shape[:3]), np.product(view_target.shape[3:]))
        view_target_flat = np.reshape(view_target, new_shape)
        view_moving_flat = np.reshape(view_moving, new_shape)
        view_mask_flat = np.reshape(view_mask, new_shape)

        # Iterate over patches
        corr_values = []
        for i in range(view_target_flat.shape[0]):

            patch_mask = view_mask_flat[i]

            patch_target = view_target_flat[i, :][np.where(patch_mask)]
            patch_moving = view_moving_flat[i, :][np.where(patch_mask)]

            # Calculate Pearson correlation coefficient
            sr, _ = pearsonr(patch_target, patch_moving)
            corr_values.append(sr)

        # Reshape into the image
        corr_arr = np.reshape(corr_values, img_target_res.GetSize()[::-1])
        corr_arr[np.isnan(corr_arr)] = 0

        # Copy information
        corr_img = sitk.GetImageFromArray(corr_arr)
        corr_img.CopyInformation(img_target_res)

        corr_img = sitk.Resample(corr_img, target_image)

        # We need all positive values for the weight map
        # We can take the absolute value, or just add one
        # Abs: makes sense for multimodality (MR-CT)
        # +1: makes sense for similar modality (CT-cCT)
        correlation_function = vote_params["correlation_function"]

        return correlation_function(corr_img)

    square_difference_image = sitk.SquaredDifference(target_image, moving_image)
    square_difference_image = sitk.Cast(square_difference_image, sitk.sitkFloat32)

    if vote_type.lower() == "unweighted":
        weight_map = target_image * 0.0 + 1.0

    elif vote_type.lower() == "global":
        factor = vote_params["factor"]
        sum_squared_difference = sitk.GetArrayFromImage(square_difference_image).sum(
            dtype=np.float
        )
        global_weight = factor / sum_squared_difference

        weight_map = target_image * 0.0 + global_weight

    elif vote_type.lower() == "local":
        sigma = vote_params["sigma"]
        epsilon = vote_params["epsilon"]
        normalise = vote_params["normalise"]

        raw_map = sitk.DiscreteGaussian(square_difference_image, sigma * sigma)
        weight_map = sitk.Pow(raw_map + epsilon, -1.0)

        if isinstance(normalise, bool):
            if normalise:
                weight_map = weight_map / sitk.GetArrayViewFromImage(weight_map).max()
        if isinstance(normalise, sitk.Image):
            weight_map = (
                weight_map / sitk.GetArrayViewFromImage(sitk.Mask(weight_map, normalise)).max()
            )

    elif vote_type.lower() == "block":
        factor = vote_params["factor"]
        gain = vote_params["gain"]
        block_size = vote_params["blockSize"]
        normalise = vote_params["normalise"]

        if isinstance(block_size, int):
            block_size = (block_size,) * target_image.GetDimension()

        # rawMap = sitk.Mean(square_difference_image, blockSize)
        raw_map = sitk.BoxMean(square_difference_image, block_size)
        weight_map = factor * sitk.Pow(raw_map, -1.0) ** abs(gain / 2.0)
        # Note: we divide gain by 2 to account for using the squared difference image
        #       which raises the power by 2 already.

        if isinstance(normalise, bool):
            if normalise:
                weight_map = weight_map / sitk.GetArrayViewFromImage(weight_map).max()
        if isinstance(normalise, sitk.Image):
            weight_map = (
                weight_map / sitk.GetArrayViewFromImage(sitk.Mask(weight_map, normalise)).max()
            )

    else:
        raise ValueError("Weighting scheme not valid.")

    return sitk.Cast(weight_map, sitk.sitkFloat32)


def combine_labels_staple(label_list_dict, threshold=1e-4):
    """
    Combine labels using STAPLE
    """

    combined_label_dict = {}

    structure_name_list = [list(i.keys()) for i in label_list_dict.values()]
    structure_name_list = np.unique([item for sublist in structure_name_list for item in sublist])

    for structure_name in structure_name_list:
        # Ensure all labels are binarised
        binary_labels = [
            sitk.BinaryThreshold(label_list_dict[i][structure_name], lowerThreshold=0.5)
            for i in label_list_dict
        ]

        # Perform STAPLE
        combined_label = sitk.STAPLE(binary_labels)

        # Normalise
        combined_label = sitk.RescaleIntensity(combined_label, 0, 1)

        # Threshold - grants vastly improved compression performance
        if threshold:
            combined_label = sitk.Threshold(
                combined_label, lower=threshold, upper=1, outsideValue=0.0
            )

        combined_label_dict[structure_name] = combined_label

    return combined_label_dict


def combine_labels(atlas_set, structure_name, label="DIR", threshold=1e-4, smooth_sigma=1.0):
    """
    Combine labels using weight maps
    """

    case_id_list = list(atlas_set.keys())

    if isinstance(structure_name, str):
        structure_name_list = [structure_name]
    elif isinstance(structure_name, list):
        structure_name_list = structure_name

    combined_label_dict = {}

    for s_name in structure_name_list:
        # Find the cases which have the strucure (in case some cases do not)
        valid_case_id_list = [i for i in case_id_list if s_name in atlas_set[i][label].keys()]

        # Get valid weight images
        weight_image_list = [
            atlas_set[caseId][label]["Weight Map"] for caseId in valid_case_id_list
        ]

        # Sum the weight images
        weight_sum_image = reduce(lambda x, y: x + y, weight_image_list)
        weight_sum_image = sitk.Mask(
            weight_sum_image, weight_sum_image == 0, maskingValue=1, outsideValue=1
        )

        # Combine weight map with each label
        weighted_labels = [
            atlas_set[caseId][label]["Weight Map"]
            * sitk.Cast(atlas_set[caseId][label][s_name], sitk.sitkFloat32)
            for caseId in valid_case_id_list
        ]

        # Combine all the weighted labels
        combined_label = reduce(lambda x, y: x + y, weighted_labels) / weight_sum_image

        # Smooth combined label
        combined_label = sitk.DiscreteGaussian(combined_label, smooth_sigma * smooth_sigma)

        # Normalise
        combined_label = sitk.RescaleIntensity(combined_label, 0, 1)

        # Threshold - grants vastly improved compression performance
        if threshold:
            combined_label = sitk.Threshold(
                combined_label, lower=threshold, upper=1, outsideValue=0.0
            )

        combined_label_dict[s_name] = combined_label

    return combined_label_dict


def process_probability_image(probability_image, threshold=0.5):
    """
    Generate a mask given a probability image, performing some basic post processing as well.
    """

    # Check type
    if not isinstance(probability_image, sitk.Image):
        probability_image = sitk.GetImageFromArray(probability_image)

    # Normalise probability map
    probability_image = probability_image / sitk.GetArrayFromImage(probability_image).max()

    # Get the starting binary image
    binary_image = sitk.BinaryThreshold(probability_image, lowerThreshold=threshold)

    # Fill holes
    binary_image = sitk.BinaryFillhole(binary_image)

    # Apply the connected component filter
    labelled_image = sitk.ConnectedComponent(binary_image)

    # Measure the size of each connected component
    label_shape_filter = sitk.LabelShapeStatisticsImageFilter()
    label_shape_filter.Execute(labelled_image)
    label_indices = label_shape_filter.GetLabels()
    voxel_counts = [label_shape_filter.GetNumberOfPixels(i) for i in label_indices]
    if voxel_counts == []:
        return binary_image

    # Select the largest region
    largest_component_label = label_indices[np.argmax(voxel_counts)]
    largest_component_image = labelled_image == largest_component_label

    return sitk.Cast(largest_component_image, sitk.sitkUInt8)
