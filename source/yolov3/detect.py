import argparse
import torch
from sys import platform

from .models import *  # set ONNX_EXPORT in models.py
from .utils.datasets import *
from .utils.utils import *

import matplotlib.pyplot as plt


class Detect:
    def __init__(self, device, cfg, weights, img_size=416, output=None, half=False, view_img=False,
                 classes: dict = None):
        self.device = device
        self.cfg = cfg
        self.weights = weights
        self.img_size = (320, 192) if ONNX_EXPORT else img_size
        self.half = False
        self.view_img = view_img
        self.classes = classes
        self.colors = [[random.randint(0, 255) for _ in range(3)] for _ in range(len(classes))]

        self.model = self.get_model()

    def get_model(self):
        model = Darknet(self.cfg, self.img_size)
        attempt_download(self.weights)
        if self.weights.endswith('.pt'):  # pytorch format
            model.load_state_dict(torch.load(self.weights, map_location=self.device)['model'])
        else:  # darknet format
            _ = load_darknet_weights(model, self.weights)

        model.to(self.device).eval()

        # Half precision
        self.half = self.half and self.device.type != 'cpu'  # half precision only supported on CUDA
        if self.half:
            model.half()

        return model

    def __call__(self, img, conf_thres=0.3, nms_thres=0.5):
        t = time.time()
        # Padded resize
        img_pad = letterbox(img)[0]
        # Normalize RGB
        img_pad = cv2.cvtColor(img_pad, cv2.COLOR_BGR2RGB)
        img_pad = img_pad.transpose(2, 0, 1)
        img_pad = np.ascontiguousarray(img_pad, dtype=np.float16 if self.half else np.float32)  # uint8 to fp16/fp32
        img_pad /= 255.0  # 0 - 255 to 0.0 - 1.0

        x = torch.from_numpy(img_pad).to(self.device)
        if x.ndimension() == 3:
            x = x.unsqueeze(0)
        pred = self.model(x)[0]
        if self.half:
            pred = pred.float()
        pred = non_max_suppression(pred, conf_thres, nms_thres)
        # print(f'Pred done in {time.time() - t:.3f}s')
        for i, det in enumerate(pred):  # detections per image
            if det is not None and len(det):
                det[:, :4] = scale_coords(x.shape[2:], det[:, :4], img.shape).round()
                for *xyxy, conf, _, cls in det:
                    # Rescale boxes from img_size to im0 size
                    # print(f'class={self.classes[int(cls)]:<10} coords={xyxy}')
                    if self.view_img:
                        label = '%s %.2f' % (self.classes[int(cls)], conf)
                        plot_one_box(xyxy, img, label=label, color=self.colors[int(cls)])

        if self.view_img:
            fig = plt.figure()
            plt.imshow(img)
            plt.show()

    def __repr__(self):
        return f'Detector(device=({self.device}))'


def detect(save_txt=False, save_img=False):
    out, source, weights, half, view_img = opt.output, opt.source, opt.weights, opt.half, opt.view_img
    webcam = source == '0' or source.startswith('rtsp') or source.startswith('http') or source.endswith('.txt')

    # Initialize
    device = torch_utils.select_device(device='cpu' if ONNX_EXPORT else opt.device)
    if os.path.exists(out):
        shutil.rmtree(out)  # delete output folder
    os.makedirs(out)  # make new output folder

    # Initialize model
    model = Darknet(opt.cfg, opt.img_size)

    # Load weights
    attempt_download(weights)
    if weights.endswith('.pt'):  # pytorch format
        model.load_state_dict(torch.load(weights, map_location=device)['model'])
    else:  # darknet format
        _ = load_darknet_weights(model, weights)

    # Second-stage classifier
    classify = False
    if classify:
        modelc = torch_utils.load_classifier(name='resnet101', n=2)  # initialize
        modelc.load_state_dict(torch.load('weights/resnet101.pt', map_location=device)['model'])  # load weights
        modelc.to(device).eval()

    # Fuse Conv2d + BatchNorm2d layers
    # model.fuse()

    # Eval mode
    model.to(device).eval()

    # Export mode
    if ONNX_EXPORT:
        img = torch.zeros((1, 3) + img_size)  # (1, 3, 320, 192)
        torch.onnx.export(model, img, 'weights/export.onnx', verbose=False, opset_version=11)

        # Validate exported model
        import onnx
        model = onnx.load('weights/export.onnx')  # Load the ONNX model
        onnx.checker.check_model(model)  # Check that the IR is well formed
        print(onnx.helper.printable_graph(model.graph))  # Print a human readable representation of the graph
        return

    # Half precision
    half = half and device.type != 'cpu'  # half precision only supported on CUDA
    if half:
        model.half()

    # Set Dataloader
    vid_path, vid_writer = None, None
    if webcam:
        view_img = True
        torch.backends.cudnn.benchmark = True  # set True to speed up constant image size inference
        dataset = LoadStreams(source, img_size=img_size, half=half)
    else:
        save_img = True
        dataset = LoadImages(source, img_size=img_size, half=half)

    # Get classes and colors
    classes = load_classes(parse_data_cfg(opt.data)['names'])
    colors = [[random.randint(0, 255) for _ in range(3)] for _ in range(len(classes))]

    # Run inference
    t0 = time.time()
    for path, img, im0s, vid_cap in dataset:
        t = time.time()

        # Get detections
        img = torch.from_numpy(img).to(device)
        if img.ndimension() == 3:
            img = img.unsqueeze(0)
        pred = model(img)[0]

        if opt.half:
            pred = pred.float()

        # Apply NMS
        pred = non_max_suppression(pred, opt.conf_thres, opt.nms_thres)

        # Apply
        if classify:
            pred = apply_classifier(pred, modelc, img, im0s)

        # Process detections
        for i, det in enumerate(pred):  # detections per image
            if webcam:  # batch_size >= 1
                p, s, im0 = path[i], '%g: ' % i, im0s[i]
            else:
                p, s, im0 = path, '', im0s

            save_path = str(Path(out) / Path(p).name)
            s += '%gx%g ' % img.shape[2:]  # print string
            if det is not None and len(det):
                # Rescale boxes from img_size to im0 size
                det[:, :4] = scale_coords(img.shape[2:], det[:, :4], im0.shape).round()

                # Print results
                for c in det[:, -1].unique():
                    n = (det[:, -1] == c).sum()  # detections per class
                    s += '%g %ss, ' % (n, classes[int(c)])  # add to string

                # Write results
                for *xyxy, conf, _, cls in det:
                    if save_txt:  # Write to file
                        with open(save_path + '.txt', 'a') as file:
                            file.write(('%g ' * 6 + '\n') % (*xyxy, cls, conf))

                    if save_img or view_img:  # Add bbox to image
                        label = '%s %.2f' % (classes[int(cls)], conf)
                        plot_one_box(xyxy, im0, label=label, color=colors[int(cls)])

            print('%sDone. (%.3fs)' % (s, time.time() - t))

            # Stream results
            if view_img:
                cv2.imshow(p, im0)

            # Save results (image with detections)
            if save_img:
                if dataset.mode == 'images':
                    cv2.imwrite(save_path, im0)
                else:
                    if vid_path != save_path:  # new video
                        vid_path = save_path
                        if isinstance(vid_writer, cv2.VideoWriter):
                            vid_writer.release()  # release previous video writer

                        fps = vid_cap.get(cv2.CAP_PROP_FPS)
                        w = int(vid_cap.get(cv2.CAP_PROP_FRAME_WIDTH))
                        h = int(vid_cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
                        vid_writer = cv2.VideoWriter(save_path, cv2.VideoWriter_fourcc(*opt.fourcc), fps, (w, h))
                    vid_writer.write(im0)

    if save_txt or save_img:
        print('Results saved to %s' % os.getcwd() + os.sep + out)
        if platform == 'darwin':  # MacOS
            os.system('open ' + out + ' ' + save_path)

    print('Done. (%.3fs)' % (time.time() - t0))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--cfg', type=str, default='cfg/yolov3-spp.cfg', help='cfg file path')
    parser.add_argument('--data', type=str, default='data/coco.data', help='coco.data file path')
    parser.add_argument('--weights', type=str, default='weights/yolov3-spp.weights', help='path to weights file')
    parser.add_argument('--source', type=str, default='data/samples', help='source')  # input file/folder, 0 for webcam
    parser.add_argument('--output', type=str, default='output', help='output folder')  # output folder
    parser.add_argument('--img-size', type=int, default=416, help='inference size (pixels)')
    parser.add_argument('--conf-thres', type=float, default=0.3, help='object confidence threshold')
    parser.add_argument('--nms-thres', type=float, default=0.5, help='iou threshold for non-maximum suppression')
    parser.add_argument('--fourcc', type=str, default='mp4v', help='output video codec (verify ffmpeg support)')
    parser.add_argument('--half', action='store_true', help='half precision FP16 inference')
    parser.add_argument('--device', default='', help='device id (i.e. 0 or 0,1) or cpu')
    parser.add_argument('--view-img', action='store_true', help='display results')
    opt = parser.parse_args()
    print(opt)

    opt.cfg = 'cfg/yolov3-tiny-frames.cfg'
    opt.data = '/home/francesco/Documents/Kanga-Challenge/source/dataset/yolo/frames.data'
    opt.weights = 'weights/best.pt'
    classes = {0: 'player', 1: 'time', 2: 'stocks', 3: 'damage'}
    # datector = Detect(torch.device('cuda'), opt, classes=classes)
    datector = Detect(torch.device('cpu'),
                      img_size=opt.img_size,
                      source=opt.source,
                      output=opt.output,
                      weights=opt.weights,
                      half=opt.half,
                      view_img=opt.view_img,
                      cfg=opt.cfg,
                      classes=classes)
    img = cv2.imread('./data/samples/830.jpg')
    # img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    with torch.no_grad():
        datector(img)
    # with torch.no_grad():
    #     detect()