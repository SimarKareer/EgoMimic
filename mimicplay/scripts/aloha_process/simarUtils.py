import numpy as np
import cv2
import matplotlib.pyplot as plt

def is_key(x):
    return hasattr(x, 'keys') and callable(x.keys)

def is_listy(x):
    return isinstance(x, list)


def nested_ds_print(nested_ds, tab_level=0):
    """
    Print the structure of a nested dataset.
    nested_ds: a series of nested dictionaries and iterables.  If a dictionary, print the key and recurse on the value.  If a list, print the length of the list and recurse on just the first index.  For other types, just print the shape.
    """
    # print('--' * tab_level, end='')
    if is_key(nested_ds):
        print("dict with keys: ", nested_ds.keys())
    elif is_listy(nested_ds):
        print("list of len: ", len(nested_ds))
    else:
        # print('\t' * (tab_level), end='')
        print(nested_ds.shape)

    if is_key(nested_ds):
        for key, value in nested_ds.items():
            print('\t' * (tab_level), end='')
            print(f"{key}: ", end="")
            nested_ds_print(value, tab_level + 1)
    elif isinstance(nested_ds, list):
        print('\t' * tab_level, end='')
        print("Index[0]", end="")
        nested_ds_print(nested_ds[0], tab_level+1)


def ee_pose_to_cam_pixels(ee_pose_base, T_cam_base, intrinsics):
    """

    """
    ee_pose_base = np.concatenate([ee_pose_base, np.array([1])], axis=0)
    print("3d pos in base frame: ", ee_pose_base)

    ee_pose_grip_cam = np.linalg.inv(T_cam_base) @ ee_pose_base
    print("3d pos in cam frame: ", ee_pose_grip_cam)

    px_val = intrinsics @ ee_pose_grip_cam
    px_val = px_val / px_val[2]
    print("2d pos cam frame: ", px_val)

    return px_val

def cam_frame_to_cam_pixels(ee_pose_cam, intrinsics):
    """
        camera frame 3d coordinates to pixels in camera frame
        ee_pose_cam: [x, y, z]
        intrinsics: 3x4 matrix
    """
    ee_pose_cam = np.concatenate([ee_pose_cam, np.array([1])], axis=0)
    # print("3d pos in cam frame: ", ee_pose_cam)

    px_val = intrinsics @ ee_pose_cam
    px_val = px_val / px_val[2]
    # print("2d pos cam frame: ", px_val)

    return px_val

def draw_dot_on_frame(frame, pixel_vals, show=True, palette="Purples"):
    frame = frame.astype(np.uint8).copy()
    if isinstance(pixel_vals, tuple):
        pixel_vals = [pixel_vals]

    # get purples color palette, and color the circles accordingly
    color_palette = plt.get_cmap(palette)
    color_palette = color_palette(np.linspace(0, 1, len(pixel_vals)))
    color_palette = (color_palette[:, :3] * 255).astype(np.uint8)
    color_palette = color_palette.tolist()


    for i, pixel_val in enumerate(pixel_vals):
        frame = cv2.circle(frame, (int(pixel_val[0]), int(pixel_val[1])), 5, color_palette[i], -1)
        if show:
            plt.imshow(frame)
            plt.show()

    return frame


def general_norm(array, min_val, max_val, arr_min=None, arr_max=None):
    if arr_min is None:
        arr_min = array.min()
    if arr_max is None:
        arr_max = array.max()
    
    return (max_val - min_val) * ((array - arr_min) / (arr_max - arr_min)) + min_val

def general_unnorm(array, orig_min, orig_max, min_val, max_val):
    return ((array - min_val) / (max_val - min_val)) * (orig_max - orig_min) + orig_min