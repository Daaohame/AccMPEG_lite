"""
    Train the NN-basedmask generator.
"""

import argparse
import glob
import importlib.util
import logging
import math
import os
import random
from pathlib import Path
from pdb import set_trace

import coloredlogs
import enlighten
import matplotlib.pyplot as plt
import numpy as np
import seaborn as sns
import torch
import torch.distributed as dist
import torch.nn as nn
import torch.nn.functional as F
import torchvision.transforms as T
from PIL import Image
from sklearn.mixture import GaussianMixture
from torch.utils.data import DataLoader, Dataset
from torch.utils.tensorboard import SummaryWriter
from torchvision import io
from torchvision.datasets import CocoDetection

from dnn.fasterrcnn_resnet50 import FasterRCNN_ResNet50_FPN
from utils.bbox_utils import center_size
from utils.loss_utils import cross_entropy as get_loss
from utils.mask_utils import *
from utils.results_utils import read_results
from utils.video_utils import get_qp_from_name, read_videos, write_video
from utils.visualize_utils import visualize_heat

sns.set()

thresholds = [1, 0]
weight = [1, 1]

path2data = "/tank/kuntai/COCO_Detection/train2017"
path2json = "/tank/kuntai/COCO_Detection/annotations/instances_train2017.json"


# def transform(image):
#     w, h = image.size
#     padh = (h + args.tile_size - 1) // args.tile_size * args.tile_size - h
#     padw = (w + args.tile_size - 1) // args.tile_size * args.tile_size - w
#     pad = T.Pad((0, 0, padh, padw), fill=(123, 116, 103))
#     return T.ToTensor()(pad(image))


class COCO_Dataset(Dataset):
    def __init__(self):
        self.path = "/tank/kuntai/COCO_Detection/train2017_reorder/"
        self.len = len(glob.glob(self.path + "*.jpg"))

    def __len__(self):
        return self.len

    def __getitem__(self, idx):
        image = Image.open(self.path + "%010d.jpg" % idx).convert("RGB")

        w, h = image.size
        if h > w:
            return None
        transform = T.Compose(
            [
                # T.Pad(
                #     (
                #         math.floor((1280 - w) / 2),
                #         math.floor((720 - h) / 2),
                #         math.ceil((1280 - w) / 2),
                #         math.ceil((720 - h) / 2),
                #     ),
                #     fill=(123, 116, 103),
                # ),
                T.Resize((720, 1280)),
                T.ToTensor(),
            ]
        )
        image = transform(image)

        return {"image": image, "fid": idx}


def my_collate(batch):
    batch = list(filter(lambda x: x is not None, batch))
    if len(batch) >= 1:
        return torch.utils.data.dataloader.default_collate(batch)
    else:
        return None


def main(args):

    # initialization for distributed training
    # dist.init_process_group(backend='nccl')
    # torch.cuda.set_device(args.local_rank)

    # initialize logger
    logger = logging.getLogger("train_COCO")
    logger.addHandler(logging.FileHandler(args.log))
    torch.set_default_tensor_type(torch.FloatTensor)
    writer = SummaryWriter("runs/" + Path(f"{args.path}").stem)

    # construct training set and cross validation set
    train_val_set = COCO_Dataset()
    train_val_set, _ = torch.utils.data.random_split(
        train_val_set,
        [math.ceil(0.2 * len(train_val_set)), math.floor(0.8 * len(train_val_set))],
        generator=torch.Generator().manual_seed(100),
    )
    training_set, cross_validation_set = torch.utils.data.random_split(
        train_val_set,
        [math.ceil(0.7 * len(train_val_set)), math.floor(0.3 * len(train_val_set))],
        generator=torch.Generator().manual_seed(100),
    )
    # training_sampler = torch.utils.data.DistributedSampler(training_set)
    training_loader = torch.utils.data.DataLoader(
        training_set,
        batch_size=args.batch_size,
        shuffle=True,
        num_workers=4,
        collate_fn=my_collate,
    )
    cross_validation_loader = torch.utils.data.DataLoader(
        cross_validation_set,
        batch_size=args.batch_size,
        num_workers=4,
        collate_fn=my_collate,
    )

    # construct the mask generator
    maskgen_spec = importlib.util.spec_from_file_location("maskgen", args.maskgen_file)
    maskgen = importlib.util.module_from_spec(maskgen_spec)
    maskgen_spec.loader.exec_module(maskgen)
    mask_generator = maskgen.FCN()
    if os.path.exists(args.path + ".best"):
        logger.info(f"Load the model from %s", args.path)
        mask_generator.load(args.path + ".best")
    mask_generator.cuda()
    mask_generator.train()
    # mask_generator = torch.nn.parallel.DistributedDataParallel(mask_generator, device_ids=[args.local_rank])

    optimizer = torch.optim.Adam(mask_generator.parameters(), lr=args.learning_rate)
    scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(optimizer, "min")

    # load ground truth results
    saliency = {}

    if Path(args.ground_truth).exists():
        with open(args.ground_truth, "rb") as f:
            saliency = pickle.load(f)
    else:
        # get the application
        # generate saliency
        application = FasterRCNN_ResNet50_FPN()
        application.cuda()
        loader = torch.utils.data.DataLoader(
            train_val_set, shuffle=False, num_workers=4, collate_fn=my_collate
        )
        progress_bar = enlighten.get_manager().counter(
            total=len(train_val_set),
            desc=f"Generating saliency as ground truths",
            unit="frames",
        )
        for thresh in thresholds:
            saliency[thresh] = {}
        for data in loader:
            progress_bar.update()
            # get data
            if data == None:
                continue
            fid = data["fid"].item()
            hq_image = data["image"].cuda(non_blocking=True)
            hq_image.requires_grad = True
            # get salinecy
            gt_result = application.inference(hq_image, nograd=False)[0]
            _, scores, boxes, _ = application.filter_results(
                gt_result, args.confidence_threshold, True, train=True
            )
            boxes = center_size(boxes.detach().cpu())
            if len(scores) == 0:
                continue
            sums = sum(scores)
            sums.backward()
            mask_grad = hq_image.grad.norm(dim=1, p=2, keepdim=True)
            mask_grad = F.conv2d(
                mask_grad,
                torch.ones([1, 1, args.tile_size, args.tile_size]).cuda(),
                stride=args.tile_size,
            )
            # determine the threshold
            mask_grad = mask_grad.detach().cpu()
            # normalize gradient to [0, 1]
            mask_grad = mask_grad - mask_grad.min()
            mask_grad = mask_grad / mask_grad.max()
            mask_grad = mask_grad.detach().cpu()

            # save it
            for thresh in thresholds:
                saliency[fid] = mask_grad.detach().cpu()

            # visualize the saliency
            if fid % 250 == 0:

                # visualize
                if args.visualize:
                    image = T.ToPILImage()(data["image"][0])
                    application.plot_results_on(
                        gt_result, image, "Azure", args, train=True
                    )

                    # plot the ground truth
                    visualize_heat(
                        image, mask_grad, f"train/{args.path}/{fid}_saliency.png", args
                    )

                    # # visualize distribution
                    # fig, ax = plt.subplots(1, 1, figsize=(11, 5), dpi=200)

                    # try:
                    #     sns.distplot(sum_mask.flatten().detach().numpy())
                    #     fig.savefig(
                    #         f"train/{args.path}/{fid}_logdist.png", bbox_inches="tight"
                    #     )
                    # except:
                    #     pass
                    # plt.close(fig)

                    # # write mean and std in gaussian mixture model
                    # with open(f"train/{args.path}/{fid}_mean_std.txt", "w") as f:
                    #     f.write(f"{mean} {std}")

        # write saliency to disk
        with open(args.ground_truth, "wb") as f:
            pickle.dump(saliency, f)

    # training
    mean_cross_validation_loss_before = 100

    for iteration in range(args.num_iterations):

        """
            Training
        """

        progress_bar = enlighten.get_manager().counter(
            total=len(training_set),
            desc=f"Iteration {iteration} on training set",
            unit="frames",
        )

        training_losses = []

        for idx, data in enumerate(training_loader):

            progress_bar.update()

            # inference
            # if not any("bbox" in _ for _ in data[1]):
            #     continue
            # fids = [data[1][0]["image_id"].item()]
            # if fids[0] not in saliency[thresholds[0]]:
            #     continue
            if data == None:
                continue
            fids = [fid.item() for fid in data["fid"]]
            if any(fid not in saliency for fid in fids):
                continue
            target = torch.cat([saliency[fid] for fid in fids]).cuda(non_blocking=True)
            hq_image = data["image"].cuda(non_blocking=True)
            mask_slice = mask_generator(hq_image).softmax(dim=1)[:, 1:2, :, :]

            # calculate loss
            loss = get_loss(mask_slice, target, 10)
            loss.backward()

            # optimization and logging
            writer.add_scalar(
                "Training loss",
                loss.item(),
                idx + iteration * (len(training_set) + len(cross_validation_set)),
            )
            training_losses.append(loss.item())
            optimizer.step()
            optimizer.zero_grad()

            if any(fid % 250 == 0 for fid in fids):
                # save the model
                mask_generator.save(args.path)
                # visualize
                if args.visualize:
                    maxid = np.argmax([fid % 250 == 0 for fid in fids]).item()
                    fid = fids[maxid]
                    image = T.ToPILImage()(data["image"][maxid])
                    # hq_image.requires_grad = True
                    # get salinecy
                    # gt_result = application.inference(hq_image.cuda(), nograd=False)[0]
                    # _, scores, boxes, _ = application.filter_results(
                    #     gt_result, args.confidence_threshold, True, train=True
                    # )
                    # sums = scores.sum()
                    # sums.backward()
                    visualize_heat(
                        image,
                        mask_slice.cpu().detach(),
                        f"train/{args.path}/{fid}_train.png",
                        args,
                    )
                    # application.plot_results_on(
                    #     gt_result, image, "Azure", args, train=True
                    # )
                    # fid = fids[0]

                    # plot the ground truth
                    # if not Path(f"train/{args.path}/{fid}_train.png").exists():
                    #     fig, ax = plt.subplots(1, 1, figsize=(11, 5), dpi=200)
                    #     sum_mask = tile_mask(
                    #         sum(saliency[thresh][fid].float() for thresh in thresholds),
                    #         args.tile_size,
                    #     )[0, 0, :, :]
                    #     ax = sns.heatmap(
                    #         sum_mask.cpu().detach().numpy(),
                    #         zorder=3,
                    #         alpha=0.5,
                    #         ax=ax,
                    #         xticklabels=False,
                    #         yticklabels=False,
                    #     )
                    #     ax.imshow(image, zorder=3, alpha=0.5)
                    #     ax.tick_params(left=False, bottom=False)
                    #     Path(f"train/{args.path}/").mkdir(parents=True, exist_ok=True)
                    #     fig.savefig(
                    #         f"train/{args.path}/{fid}_train.png", bbox_inches="tight"
                    #     )
                    #     plt.close(fig)

                    # visualize the test mask
                    # fig, ax = plt.subplots(1, 1, figsize=(11, 5), dpi=200)
                    # sum_mask = tile_mask(mask_slice_temp, args.tile_size,)[0, 0, :, :]
                    # ax = sns.heatmap(
                    #     sum_mask.cpu().detach().numpy(),
                    #     zorder=3,
                    #     alpha=0.5,
                    #     ax=ax,
                    #     xticklabels=False,
                    #     yticklabels=False,
                    # )
                    # ax.imshow(image, zorder=3, alpha=0.5)
                    # ax.tick_params(left=False, bottom=False)
                    # Path(f"train/{args.path}/").mkdir(parents=True, exist_ok=True)
                    # fig.savefig(
                    #     f"train/{args.path}/{fid}_test.png", bbox_inches="tight"
                    # )
                    # plt.close(fig)

                    # mask_grad = hq_image.grad.norm(dim=1, p=2, keepdim=True)
                    # mask_grad = F.conv2d(
                    #     mask_grad,
                    #     torch.ones([1, 1, args.tile_size, args.tile_size]).cuda(),
                    #     stride=args.tile_size,
                    # )
                    # mask_grad = tile_mask(mask_grad, args.tile_size)
                    # fig, ax = plt.subplots(1, 1, figsize=(11, 5), dpi=200)
                    # sum_mask = mask_grad[0, 0, :, :].log().cpu().detach()
                    # ax = sns.heatmap(
                    #     sum_mask.numpy(),
                    #     zorder=3,
                    #     alpha=0.5,
                    #     ax=ax,
                    #     xticklabels=False,
                    #     yticklabels=False,
                    # )
                    # ax.imshow(image, zorder=3, alpha=0.5)
                    # ax.tick_params(left=False, bottom=False)
                    # Path(f"train/{args.path}/").mkdir(parents=True, exist_ok=True)
                    # fig.savefig(
                    #     f"train/{args.path}/{fid}_saliency.png", bbox_inches="tight"
                    # )
                    # plt.close(fig)

                    # fig, ax = plt.subplots(1, 1, figsize=(11, 5), dpi=200)
                    # sns.distplot(sum_mask.flatten().detach().numpy())
                    # fig.savefig(
                    #     f"train/{args.path}/{fid}_logdist.png", bbox_inches="tight"
                    # )
                    # plt.close(fig)

        mean_training_loss = torch.tensor(training_losses).mean()
        logger.info("Average training loss: %.3f", mean_training_loss.item())

        """
            Cross validation
        """

        progress_bar = enlighten.get_manager().counter(
            total=len(cross_validation_set),
            desc=f"Iteration {iteration} on cross validation set",
            unit="frames",
        )

        cross_validation_losses = []

        for idx, data in enumerate(cross_validation_loader):

            progress_bar.update()

            # # extract data from dataloader
            # if not any("bbox" in _ for _ in data[1]):
            #     continue
            # fids = [data[1][0]["image_id"].item()]

            # if fids[0] not in saliency[thresholds[0]]:
            #     continue
            # hq_image = data[0].cuda()

            if data == None:
                continue
            fids = [fid.item() for fid in data["fid"]]
            if any(fid not in saliency for fid in fids):
                continue
            target = torch.cat([saliency[fid] for fid in fids]).cuda(non_blocking=True)
            hq_image = data["image"].cuda(non_blocking=True)

            # inference
            with torch.no_grad():
                mask_slice = mask_generator(hq_image).softmax(dim=1)[:, 1:2, :, :]

                # loss = 0
                # for idx, thresh in enumerate(thresholds):
                #     target = torch.cat(
                #         [saliency[thresh][fid].long().cuda() for fid in fids]
                #     )
                #     loss = loss + weight[idx] * get_loss(mask_slice, target, 1)
                loss = get_loss(mask_slice, target, 10)

            if any(fid % 250 == 0 for fid in fids):
                if args.visualize:
                    maxid = np.argmax([fid % 250 == 0 for fid in fids]).item()
                    fid = fids[maxid]
                    image = T.ToPILImage()(data["image"][maxid])
                    visualize_heat(
                        image,
                        mask_slice.detach().cpu(),
                        f"train/{args.path}/{fid}_cross.png",
                        args,
                    )

            # optimization and logging
            writer.add_scalar(
                "Cross validation loss",
                loss.item(),
                idx
                + iteration * (len(training_set) + len(cross_validation_set))
                + len(training_set),
            )
            cross_validation_losses.append(loss.item())

        mean_cross_validation_loss = torch.tensor(cross_validation_losses).mean().item()
        logger.info("Average cross validation loss: %.3f", mean_cross_validation_loss)

        if mean_cross_validation_loss < mean_cross_validation_loss_before:
            mask_generator.save(args.path + ".best")
        mean_cross_validation_loss_before = min(
            mean_cross_validation_loss_before, mean_cross_validation_loss
        )

        # check if we need to reduce learning rate.
        scheduler.step(mean_cross_validation_loss)


if __name__ == "__main__":

    # set the format of the logger
    coloredlogs.install(
        fmt="%(asctime)s [%(levelname)s] %(name)s:%(funcName)s[%(lineno)s] -- %(message)s",
        level="INFO",
    )

    parser = argparse.ArgumentParser()

    # parser.add_argument(
    #     "-i",
    #     "--inputs",
    #     nargs="+",
    #     help="The video file name. The largest video file will be the ground truth.",
    #     required=True,
    # )
    # parser.add_argument('-s', '--source', type=str, help='The original video source.', required=True)
    # parser.add_argument('-g', '--ground_truth', type=str,
    #                     help='The ground truth videos.', required=True)
    parser.add_argument(
        "-p",
        "--path",
        type=str,
        help="The path to store the generator parameters.",
        required=True,
    )
    parser.add_argument(
        "--log", type=str, help="The logging file.", required=True,
    )
    parser.add_argument(
        "-g", "--ground_truth", type=str, help="The ground truth file.", required=True
    )
    # parser.add_argument('-o', '--output', type=str,
    #                     help='The output name.', required=True)
    parser.add_argument(
        "--confidence_threshold",
        type=float,
        help="The confidence score threshold for calculating accuracy.",
        default=0.5,
    )
    parser.add_argument(
        "--maskgen_file",
        type=str,
        help="The file that defines the neural network.",
        required=True,
    )
    parser.add_argument(
        "--iou_threshold",
        type=float,
        help="The IoU threshold for calculating accuracy in object detection.",
        default=0.5,
    )
    parser.add_argument(
        "--saliency_threshold",
        type=float,
        help="The threshold to binarize the saliency.",
        default=0.5,
    )
    parser.add_argument(
        "--num_iterations",
        type=int,
        help="Number of iterations for optimizing the mask.",
        default=500,
    )
    parser.add_argument(
        "--batch_size",
        type=int,
        help="Number of iterations for optimizing the mask.",
        default=2,
    )
    parser.add_argument(
        "--tile_size", type=int, help="The tile size of the mask.", default=8
    )
    parser.add_argument(
        "--learning_rate", type=float, help="The learning rate.", default=1e-4
    )
    parser.add_argument(
        "--gamma", type=float, help="The gamma parameter for focal loss.", default=2
    )
    parser.add_argument(
        "--visualize", type=bool, help="Visualize the heatmap.", default=False
    )
    # parser.add_argument(
    #     "--local_rank", default=-1, type=int, help="The GPU id for distributed training"
    # )

    args = parser.parse_args()

    main(args)
