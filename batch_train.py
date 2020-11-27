import os
import subprocess
from itertools import product

# v_list = ['dashcam_%d_test' % (i+1) for i in range(4)] + ['trafficcam_%d_test' % (i+1) for i in range(4)]
# v_list = [v_list[0]]

# v_list = ['train_first/trafficcam_%d_train' % (i+1) for i in range(4)] + ['train_first/dashcam_%d_train' % (i+1) for i in range(4)]
# v_list = [v_list[4]]

model_name = "fcn_black_vis_172_mse"

subprocess.run(
    [
        "python",
        "train.py",
        "-i",
        "visdrone/videos/vis_172_qp_22.hevc",
        "-g",
        "visdrone/videos/vis_172_compressed_black.hevc.mask",
        "-p",
        f"maskgen_pths/{model_name}.pth",
        "--tile_size",
        "16",
        "--batch_size",
        "2",
        "--log",
        f"train_{model_name}.log",
    ]
)
