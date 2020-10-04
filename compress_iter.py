'''
    Compress the video based on shrinking/enlarging the regions iteratively.
'''

import torch
from torchvision import io
import argparse
import coloredlogs
import logging
import enlighten
import torchvision.transforms as T
from PIL import Image
import seaborn as sns; sns.set()
import matplotlib.pyplot as plt
from pathlib import Path

from dnn.fasterrcnn_resnet50 import FasterRCNN_ResNet50_FPN
from utils.video_utils import read_videos, write_video, get_qp_from_name
from utils.mask_utils import *
from utils.results_utils import read_results
from utils.bbox_utils import center_size


def main(args):

    # initialize
    logger = logging.getLogger('compress_iter')
    handler = logging.NullHandler()
    logger.addHandler(handler)
    torch.set_default_tensor_type(torch.FloatTensor)
    
    # read the video frames (will use the largest video as ground truth)
    videos, bws, video_names = read_videos(args.inputs, logger, sort=True)
    videos = videos
    bws = bws
    qps = [get_qp_from_name(video_name) for video_name in video_names]
    
    
    application = FasterRCNN_ResNet50_FPN()
    application.cuda()

    # construct the mask
    video_shape = videos[-1].shape
    mask_shape = [video_shape[0], 1, video_shape[2] // args.tile_size, video_shape[3] // args.tile_size]
    mask = torch.ones(mask_shape).float()
    mask.requires_grad = False

    # construct the regions
    ground_truth_results = read_results(args.ground_truth, application.name, logger)
    regions = [center_size(application.filter_results(i, args.confidence_threshold)[2]) for i in ground_truth_results.values()]
    
    # with initial w, h = 0
    for region in regions:
        region[:, 2:] = 0
    
    for iteration in range(args.num_iterations + 1):

        logger.info(f'Processing application {application.name}')
        progress_bar = enlighten.get_manager().counter(total=videos[-1].shape[0], desc=f'Iteration {iteration}: {application.name}', unit='frames')        

        total_loss = []

        for fid, (video_slices, mask_slice) in enumerate(zip(zip(*[video.split(1) for video in videos]), mask.split(1))):

            progress_bar.update()

            # construct hybrid image
            mask_slice = generate_mask_from_regions(mask_slice, regions[fid], bws[0])
            mask_slice = tile_mask(mask_slice, args.tile_size)
            masked_image = generate_masked_image(mask_slice, video_slices, bws)

            # calculate the loss
            inference_results = application.inference(masked_image.cuda(), detach=True)[0]
            index = application.get_undetected_ground_truth_index(ground_truth_results[fid], inference_results, args)

            # enlarge the undetected regions, but turn to 0 if cannot be detected.
            if iteration == args.num_iterations - 1:
                regions[fid][index, 2:] = 0
            else:
                regions[fid][index, 2:] += args.delta

            # visualization
            if fid % 5 == 0 and (iteration+1) % (args.num_iterations // 4) == 0:
                heat = tile_mask(mask[fid:fid+1, :, :, :], args.tile_size)[0, 0, :, :]
                plt.clf()
                ax = sns.heatmap(heat.detach().numpy(), zorder=3, alpha=0.5)
                image = T.ToPILImage()(video_slices[-1][0, :, :, :])
                image = application.plot_results_on(ground_truth_results[fid], image, (255, 255, 255), args)
                image = application.plot_results_on(inference_results, image, (0, 255, 255), args)
                ax.imshow(image, zorder=3, alpha=0.5)
                Path(f'visualize/{args.output}/').mkdir(parents=True, exist_ok=True)
                plt.savefig(f'visualize/{args.output}/{fid}_attn.png', bbox_inches='tight')
            
    # optimization done. No more gradients required.
    mask.requires_grad = False
    binarize_mask(mask, bws)
    write_masked_video(mask, args, qps, bws, logger)
    masked_video = generate_masked_video(mask, videos, bws, args)
    write_video(masked_video, args.output, logger)


if __name__ == '__main__':

    # set the format of the logger
    coloredlogs.install(fmt="%(asctime)s [%(levelname)s] %(name)s:%(funcName)s[%(lineno)s] -- %(message)s", level='INFO')

    parser = argparse.ArgumentParser()

    parser.add_argument('-i', '--inputs', nargs = '+', help='The video file names. The largest video file will be the ground truth.', required=True)
    parser.add_argument('-s', '--source', type=str, help='The original video source.', required=True)
    parser.add_argument('-g', '--ground_truth', type=str, help='The ground truth results.', required=True)
    parser.add_argument('-o', '--output', type=str, help='The output name.', required=True)
    parser.add_argument('--confidence_threshold', type=float, help='The confidence score threshold for calculating accuracy.', default=0.5)
    parser.add_argument('--iou_threshold', type=float, help='The IoU threshold for calculating accuracy in object detection.', default=0.5)
    parser.add_argument('--num_iterations', type=int, help='Number of iterations for optimizing the mask.', default=30)
    parser.add_argument('--tile_size', type=int, help='The tile size of the mask.', default=1)
    parser.add_argument('--delta', type=float, help='The delta to enlarge the region.', default=10)


    args = parser.parse_args()

    main(args)
