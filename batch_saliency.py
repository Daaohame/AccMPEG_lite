
import os
from itertools import product
import subprocess


# v_list = ['dashcam_%d_test' % (i+1) for i in range(4)] + ['trafficcam_%d_test' % (i+1) for i in range(4)]
# v_list = [v_list[0]]

v_list = ['train_first/trafficcam_%d_train' % (i+1) for i in range(4)] + ['train_first/dashcam_%d_train' % (i+1) for i in range(4)]
base = 34
tile = 8
perc = 1
niter = 6

for v in v_list:

    output = f'{v}_compressed_saliency_tile_{tile}_base_{base}_perc_{perc}_niter_{niter}.mp4'

    subprocess.run([
        'python', 'compress_saliency.py',
        '-i', f'youtube_videos/{v}_qp_{base}.mp4', f'youtube_videos/{v}_qp_24.mp4',
        '-g', f'youtube_videos/{v}_qp_24.mp4',
        '-s', f'youtube_videos/{v}',
        '-o', f'youtube_videos/{output}',
        '--tile_percentage', f'{perc}', 
        '--num_iterations', f'{niter}',
        '--tile_size', f'{tile}'
    ])
    # os.system(f'rm youtube_videos/{output}.qp{base}')
    os.system(f'cp youtube_videos/{v}_qp_{base}.mp4 youtube_videos/{output}.qp{base}')
    os.system(f'python inference.py -i youtube_videos/{output}')
    os.system(f'python examine.py -i youtube_videos/{output} -g youtube_videos/{v}_qp_24.mp4')

