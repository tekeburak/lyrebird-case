import argparse
import cv2
import glob
import math
import numpy as np
import os
import torch
from basicsr.archs.rrdbnet_arch import RRDBNet
from torch.nn import functional as F

def resize_image(input_image: np.array, target_size: int) -> np.array:
    # Simply resize the image to the target dimensions
    resized_image = cv2.resize(input_image, (target_size, target_size))
    
    return resized_image

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--input_path', type=str, default='test_images_sub', help='Input image or folder')
    parser.add_argument('--output_path', type=str, default='results', help='Output image folder')
    parser.add_argument(
        '--model_path',
        type=str,
        default='weights/RealESRGAN_x4plus.pth',
        help='Path to the pre-trained model')
    parser.add_argument('--batch_size', type=int, default=4, help='Batch size for image enhancement')
    parser.add_argument('--scale', type=int, default=4, help='Upsample scale factor')
    parser.add_argument('--suffix', type=str, default='out', help='Suffix of the restored image')
    parser.add_argument('--tile', type=int, default=200, help='Tile size, 0 for no tile during testing')
    parser.add_argument('--tile_pad', type=int, default=10, help='Tile padding')
    parser.add_argument('--pre_pad', type=int, default=0, help='Pre padding size at each border')
    parser.add_argument('--half', type=bool, default=False, help='Half precision')
    parser.add_argument(
        '--alpha_upsampler',
        type=str,
        default='realesrgan',
        help='The upsampler for the alpha channels. Options: realesrgan | bicubic')
    parser.add_argument(
        '--ext',
        type=str,
        default='auto',
        help='Image extension. Options: auto | jpg | png, auto means using the same extension as inputs')
    args = parser.parse_args()
    
    batch_size = args.batch_size
    os.makedirs(args.output_path, exist_ok=True)

    upsampler = RealESRGANer(scale=args.scale, 
                             model_path=args.model_path, 
                             tile=args.tile, 
                             tile_pad=args.tile_pad, 
                             pre_pad=args.pre_pad,
                             half=args.half)

    if os.path.isfile(args.input_path):
        paths = [args.input_path]
    else:
        paths = sorted([os.path.join(args.input_path, f) for f in os.listdir(args.input_path) 
                        if f.lower().endswith(('.png', '.jpg', '.jpeg', '.bmp'))])
        # paths = sorted(glob.glob(os.path.join(args.input_path, '*')))
        
    # Create batches of paths
    batches = [paths[i:i + batch_size] for i in range(0, len(paths), batch_size)]
    
    for batch_paths in batches:
        images = []
        max_ranges = []
        img_modes = []
        extensions = []
        imgnames = []

        for idx, path in enumerate(batch_paths):
            imgname, extension = os.path.splitext(os.path.basename(path))
            extensions.append(extension)
            imgnames.append(imgname)
            print('Testing', idx, imgname)

            # ------------------------------ read image ------------------------------ #
            input_img = cv2.imread(path, cv2.IMREAD_UNCHANGED).astype(np.float32)
            img = resize_image(input_img, target_size=256)
            if np.max(img) > 255:  # 16-bit image
                max_range = 65535
                print('\tInput is a 16-bit image')
            else:
                max_range = 255
            img = img / max_range
            if len(img.shape) == 2:  # gray image
                img_mode = 'L'
                img = cv2.cvtColor(img, cv2.COLOR_GRAY2RGB)
            elif img.shape[2] == 4:  # RGBA image with alpha channel
                img_mode = 'RGBA'
                alpha = img[:, :, 3]
                img = img[:, :, 0:3]
                img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
                if args.alpha_upsampler == 'realesrgan':
                    alpha = cv2.cvtColor(alpha, cv2.COLOR_GRAY2RGB)
            else:
                img_mode = 'RGB'
                img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
                
            images.append(img)
            max_ranges.append(max_range)
            img_modes.append(img_mode)
            
        # Convert lists to batches
        images = np.stack(images)

        # ------------------- process image (without the alpha channel) ------------------- #
        # Process the entire batch
        upsampler.pre_process(images)
        if args.tile:
            upsampler.tile_process()
        else:
            upsampler.process()
        output_imgs = upsampler.post_process()
        output_imgs = output_imgs.data.float().cpu().clamp_(0, 1).numpy()
        output_imgs = np.transpose(output_imgs[:, [2, 1, 0], :, :], (0, 2, 3, 1))
            
        
        # output_img = np.transpose(output_img[[2, 1, 0], :, :], (1, 2, 0))
        for idx, output_img in enumerate(output_imgs):
            img_mode = img_modes[idx]
            max_range = max_ranges[idx]
            extension = extensions[idx]
            imgname = imgnames[idx]
            
            if img_mode == 'L':
                output_img = cv2.cvtColor(output_img, cv2.COLOR_BGR2GRAY)

            # ------------------- process the alpha channel if necessary ------------------- #

            if img_mode == 'RGBA':
                if args.alpha_upsampler == 'realesrgan':
                    upsampler.pre_process(alpha)
                    if args.tile:
                        upsampler.tile_process()
                    else:
                        upsampler.process()
                    output_alpha = upsampler.post_process()
                    output_alpha = output_alpha.data.squeeze().float().cpu().clamp_(0, 1).numpy()
                    output_alpha = np.transpose(output_alpha[[2, 1, 0], :, :], (1, 2, 0))
                    output_alpha = cv2.cvtColor(output_alpha, cv2.COLOR_BGR2GRAY)
                else:
                    h, w = alpha.shape[0:2]
                    output_alpha = cv2.resize(alpha, (w * args.scale, h * args.scale), interpolation=cv2.INTER_LINEAR)

                # merge the alpha channel
                output_img = cv2.cvtColor(output_img, cv2.COLOR_BGR2BGRA)
                output_img[:, :, 3] = output_alpha

            # ------------------------------ save image ------------------------------ #
            if args.ext == 'auto':
                extension = extension[1:]
            else:
                extension = args.ext
            if img_mode == 'RGBA':  # RGBA images should be saved in png format
                extension = 'png'
            save_name = f'{imgname}_{args.suffix}.{extension}'
            save_path = os.path.join(args.output_path, save_name)
            if max_range == 65535:  # 16-bit image
                output = (output_img * 65535.0).round().astype(np.uint16)
            else:
                output = (output_img * 255.0).round().astype(np.uint8)
            cv2.imwrite(save_path, output)


class RealESRGANer():

    def __init__(self, scale, model_path, tile=0, tile_pad=10, pre_pad=10, half=True):
        self.scale = scale
        self.tile_size = tile
        self.tile_pad = tile_pad
        self.pre_pad = pre_pad
        self.half = half
        self.mod_scale = None
        self.output = None

        # initialize model
        self.device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        model = RRDBNet(num_in_ch=3, num_out_ch=3, num_feat=64, num_block=23, num_grow_ch=32)
        loadnet = torch.load(model_path)
        if 'params_ema' in loadnet:
            keyname = 'params_ema'
        else:
            keyname = 'params'
        model.load_state_dict(loadnet[keyname], strict=True)
        model.eval()
        self.model = model.to(self.device)
        if self.half:
            self.model = self.model.half()

    def pre_process(self, img):
        # Check if img is already a batch of images
        if len(img.shape) == 4:
            img = torch.from_numpy(np.transpose(img, (0, 3, 1, 2))).float()
        else:
            img = torch.from_numpy(np.transpose(img, (2, 0, 1))).float().unsqueeze(0)
        self.img = img.to(self.device)
        if self.half:
            self.img = self.img.half()

        # pre_pad
        if self.pre_pad != 0:
            self.img = F.pad(self.img, (0, self.pre_pad, 0, self.pre_pad), 'reflect')
        # mod pad
        if self.scale == 2:
            self.mod_scale = 2
        elif self.scale == 1:
            self.mod_scale = 4
        if self.mod_scale is not None:
            self.mod_pad_h, self.mod_pad_w = 0, 0
            _, _, h, w = self.img.size()
            if (h % self.mod_scale != 0):
                self.mod_pad_h = (self.mod_scale - h % self.mod_scale)
            if (w % self.mod_scale != 0):
                self.mod_pad_w = (self.mod_scale - w % self.mod_scale)
            self.img = F.pad(self.img, (0, self.mod_pad_w, 0, self.mod_pad_h), 'reflect')

    def process(self):
        try:
            # inference
            with torch.no_grad():
                self.output = self.model(self.img)
        except Exception as error:
            print('Error', error)

    def tile_process(self):
        """Modified from: https://github.com/ata4/esrgan-launcher
        """
        batch, channel, height, width = self.img.shape
        output_height = int(height * self.scale)
        output_width = int(width * self.scale)
        output_shape = (batch, channel, output_height, output_width)

        # start with black image
        self.output = self.img.new_zeros(output_shape)
        tiles_x = math.ceil(width / self.tile_size)
        tiles_y = math.ceil(height / self.tile_size)

        # loop over all tiles
        for y in range(tiles_y):
            for x in range(tiles_x):
                # extract tile from input image
                ofs_x = x * self.tile_size
                ofs_y = y * self.tile_size
                # input tile area on total image
                input_start_x = ofs_x
                input_end_x = min(ofs_x + self.tile_size, width)
                input_start_y = ofs_y
                input_end_y = min(ofs_y + self.tile_size, height)

                # input tile area on total image with padding
                input_start_x_pad = max(input_start_x - self.tile_pad, 0)
                input_end_x_pad = min(input_end_x + self.tile_pad, width)
                input_start_y_pad = max(input_start_y - self.tile_pad, 0)
                input_end_y_pad = min(input_end_y + self.tile_pad, height)

                # input tile dimensions
                input_tile_width = input_end_x - input_start_x
                input_tile_height = input_end_y - input_start_y
                tile_idx = y * tiles_x + x + 1
                input_tile = self.img[:, :, input_start_y_pad:input_end_y_pad, input_start_x_pad:input_end_x_pad]

                # upscale tile
                try:
                    with torch.no_grad():
                        output_tile = self.model(input_tile)
                except Exception as error:
                    print('Error', error)
                print(f'\tTile {tile_idx}/{tiles_x * tiles_y}')

                # output tile area on total image
                output_start_x = int(input_start_x * self.scale)
                output_end_x = int(input_end_x * self.scale)
                output_start_y = int(input_start_y * self.scale)
                output_end_y = int(input_end_y * self.scale)

                # output tile area without padding
                output_start_x_tile = int((input_start_x - input_start_x_pad) * self.scale)
                output_end_x_tile = int(output_start_x_tile + input_tile_width * self.scale)
                output_start_y_tile = int((input_start_y - input_start_y_pad) * self.scale)
                output_end_y_tile = int(output_start_y_tile + input_tile_height * self.scale)

                # put tile into output image
                self.output[:, :, output_start_y:output_end_y,
                            output_start_x:output_end_x] = output_tile[:, :, output_start_y_tile:output_end_y_tile,
                                                                       output_start_x_tile:output_end_x_tile]

    def post_process(self):
        # remove extra pad
        if self.mod_scale is not None:
            _, _, h, w = self.output.size()
            self.output = self.output[:, :, 0:h - self.mod_pad_h * self.scale, 0:w - self.mod_pad_w * self.scale]
        # remove prepad
        if self.pre_pad != 0:
            _, _, h, w = self.output.size()
            self.output = self.output[:, :, 0:h - self.pre_pad * self.scale, 0:w - self.pre_pad * self.scale]
        return self.output


if __name__ == '__main__':
    main()
