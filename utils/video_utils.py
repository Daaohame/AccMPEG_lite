
import torch
from torchvision import io
import os
import glob
from . import mask_utils as mu
import subprocess
from pathlib import Path
from torch.utils.data import Dataset, DataLoader
import matplotlib.pyplot as plt
import torchvision.transforms as T
import subprocess

class Video(Dataset):

    def __init__(self, video, logger):
        self.video = video
        logger.info(f'Extract {video} to pngs.')
        Path(f'{video}.pngs').mkdir(exist_ok=True)
        subprocess.run([
            'rm',
            f'{video}.pngs/*.png'
        ], stderr=subprocess.DEVNULL)
        subprocess.check_output([
            'ffmpeg',
            '-y',
            '-i', f'{video}',
            '-start_number', '0',
            f'{video}.pngs/%010d.png'
        ])
        self.nimages = len(glob.glob(f'{video}.pngs/*.png'))

    def __len__(self):
        return self.nimages

    def __getitem__(self, idx):
        image = T.ToTensor()(plt.imread(f'{self.video}.pngs/%010d.png' % idx))
        return image


def read_videos(video_list, logger, sort=False, normalize=True):
    '''
        Read a list of video and return two lists. 
        One is the video tensors, the other is the bandwidths.
    '''
    video_list = [{'video': read_video(video_name, logger),
                   'bandwidth': read_bandwidth(video_name),
                   'name': video_name}
                  for video_name in video_list]
    if sort:
        video_list = sorted(video_list, key=lambda x: x['bandwidth'])

    # bandwidth normalization
    gt_bandwidth = max(video['bandwidth'] for video in video_list)
    if normalize:
        for i in video_list:
            i['bandwidth'] /= gt_bandwidth

    return [i['video'] for i in video_list], [i['bandwidth'] for i in video_list], [i['name'] for i in video_list]

def read_video(video_name, logger):
    logger.info(f'Reading {video_name}')
    return DataLoader(Video(video_name, logger), shuffle=False, num_workers=2)

def read_bandwidth(video_name):
    return os.path.getsize(video_name)


def write_video(video_tensor, video_name, logger):

    logger.info(f'Saving {video_name}')

    # [N, C, H, W] ==> [N, H, W, C]
    video_tensor = video_tensor.permute(0, 2, 3, 1)
    # go back to original domain
    video_tensor = video_tensor.mul(255).add_(0.5).clamp_(0, 255).to('cpu', torch.uint8)
    # lossless encode. Should be replaced
    io.write_video(video_name, video_tensor, fps=25, options={'crf': '0'})

def get_qp_from_name(video_name):

    # the video name format must be xxxxxxx_{qp}.mp4
    return int(video_name.split('.')[-2].split('_')[-1])

def encode_with_qp(video_src, video_dst, qp, args):

    import struct

    # construct roi.txt
    qp_delta = qp - 22

    width = 1280 // args.tile_size
    height = 720 // args.tile_size

    #ffprobe -v error -select_streams v:0 -show_entries stream=nb_frames -of default=nokey=1:noprint_wrappers=1 input.mp4
    subprocess.run([
        'ffmpeg', '-y',
        '-f', 'rawvideo',
        '-pix_fmt', 'yuv420p',
        '-s:v', '1280x720',
        '-i', video_src,
        'temp.mp4'
    ])
    nframes = int(subprocess.check_output([
        'ffprobe',
        '-v', 'error',
        '-select_streams', 'v:0',
        '-show_entries', 'stream=nb_frames',
        '-of', 'default=nokey=1:noprint_wrappers=1',
        'temp.mp4'
    ]))

    with open('roi.dat', 'wb') as f:
        for fid in range(nframes):
            f.write(struct.pack('i', width))
            f.write(struct.pack('i', height))
            for j in range(height):
                for i in range(width):
                    f.write(struct.pack('b', qp_delta))

    # encode through kvazaar roi
    subprocess.run([
        'kvazaar',
        '--input', video_src,
        '--gop', '0',
        '--input-res', '1280x720',
        '--roi-file', 'roi.dat',
        '--output', video_dst,
    ])