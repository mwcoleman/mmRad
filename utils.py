import torch, os, csv, base64, time
import cv2
import json
import matplotlib.pyplot as plt
import numpy as np
from torch.utils.data import Dataset

from detectron2.modeling import build_model
from detectron2.checkpoint import DetectionCheckpointer
from detectron2.structures.image_list import ImageList
from detectron2.data import transforms as T
from detectron2.modeling.box_regression import Box2BoxTransform
from detectron2.modeling.roi_heads.fast_rcnn import FastRCNNOutputs
from detectron2.structures.boxes import Boxes
from detectron2.layers import nms
from detectron2 import model_zoo
from detectron2.config import get_cfg
from detectron2.utils.visualizer import Visualizer



# Custom collate for dataloader to keep images in list
# due to ragged shape
def collate_func(batch, dset='coco'):
    
    images = [b['image'] for b in batch]
    captions = [b['caption'] for b in batch]
    ids = [b['img_id'] for b in batch]
    collated_batch = {'images':images, 'captions':captions, 'img_ids':ids}

    if dset=='cub':
        collated_batch['labels'] = [b['label'] for b in batch]
    
    return collated_batch

class Extractor:
    def __init__(self, cfg_path, batch_size,num_proposals=36,custom_model=False):
        # TODO: args params
        # NMS params - LXMERT uses 36 features
        self.min_boxes = num_proposals
        self.max_boxes = num_proposals
        
        # Start with copy of default config
        self.cfg = get_cfg()
        self.cfg.merge_from_file(model_zoo.get_config_file(cfg_path))
        self.cfg.MODEL.ROI_HEADS.SCORE_THRESH_TEST = 0.5
        if not custom_model:
            self.cfg.MODEL.WEIGHTS = model_zoo.get_checkpoint_url(cfg_path)
        else:
            self.cfg.MODEL.WEIGHTS = custom_model
        # self.cfg.DATASETS.PRECOMPUTED_PROPOSAL_TOPK_TEST = num_proposals
        self.batch_size=batch_size
        # build model
        self.model = build_model(self.cfg)
        # load weights
        self.checkpointer = DetectionCheckpointer(self.model)
        self.checkpointer.load(self.cfg.MODEL.WEIGHTS)
        # Eval mode
        self.model.eval()

        self.device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

    def __call__(self, samples):
        """The ResNet model in combination with FPN generates five features 
        for an image at different levels of complexity. 
        For more details, refer to the FPN paper or this 
        (https://medium.com/@hirotoschwert/digging-into-detectron-2-47b2e794fabd).
        """
        # features.keys() = [`p2`, `p3`, `p4`, `p5`, `p6`]
        # each are featres needed by RPN. Then p2-5 + RPN proposals fed to ROI
        # for boxes
        images = samples[0]
        batched_inputs = samples[1]
        features = self.model.backbone(images.tensor)

        # Get RPs from the features and image. Based on our config, we get 1000 proposal
        proposals, _ = self.model.proposal_generator(images, features)

        # Reduce all proposals to min found for an image (not great)
        num_proposals = min(len(p) for p in proposals)
        if num_proposals < 1000:
            print(f"Only {num_proposals} generated in this batch")
            proposals = [p[:num_proposals] for p in proposals]

        # We want box_features to be the fc2 outputs of the regions, 
        # so only use the layers that are needed up to that step
        features_list = [features[f] for f in ['p2', 'p3', 'p4', 'p5']]
        box_features_1 = self.model.roi_heads.box_pooler(features_list, 
                                                      [x.proposal_boxes for x in proposals])
        box_features = self.model.roi_heads.box_head.flatten(box_features_1)
        box_features = self.model.roi_heads.box_head.fc1(box_features)
        box_features = self.model.roi_heads.box_head.fc_relu1(box_features)
        box_features = self.model.roi_heads.box_head.fc2(box_features)
        # Depends on config and batch size of images.
        # Might not be 1000 proposals
        box_features = box_features.reshape(self.batch_size, -1, 1024)

        # To get the boxes and scores from the FastRCNNOutputs 
        # we want the prediction logits and boxes:  
        cls_features = self.model.roi_heads.box_head(box_features_1)
        pred_class_logits, pred_proposal_deltas = self.model.roi_heads.box_predictor(cls_features)
        
        # To get the FastRCNN scores and boxes (softmax) we need to do this
        box2box_transform = Box2BoxTransform(weights=self.cfg.MODEL.ROI_BOX_HEAD.BBOX_REG_WEIGHTS)
        smooth_l1_beta = self.cfg.MODEL.ROI_BOX_HEAD.SMOOTH_L1_BETA

        outputs = FastRCNNOutputs(
            box2box_transform,
            pred_class_logits,
            pred_proposal_deltas,
            proposals,
            smooth_l1_beta,
        )

        boxes = outputs.predict_boxes() 
        scores = outputs.predict_probs()
        image_shapes = outputs.image_shapes

        # boxes need to be rescaled to original image size
        # TODO: Temporary Loop. Convert to vectorised if slow
        def get_output_boxes(boxes, batched_inputs, image_size, scores):
            proposal_boxes = boxes.reshape(-1, 4).clone()
            scale_x, scale_y = (batched_inputs["width"] / image_size[1], batched_inputs["height"] / image_size[0])
            output_boxes = Boxes(proposal_boxes)

            output_boxes.scale(scale_x, scale_y)
            output_boxes.clip(image_size)

            # Select the Boxes using NMS
            # We need two thresholds - NMS threshold for the NMS box section, and score threshold for the score based section.
            # First NMS is performed for all the classes and the max scores of each proposal box and each class is updated.
            # Then the class score threshold is used to select the boxes from those.
            test_score_thresh = self.cfg.MODEL.ROI_HEADS.SCORE_THRESH_TEST
            test_nms_thresh = self.cfg.MODEL.ROI_HEADS.NMS_THRESH_TEST
            cls_prob = scores.detach()
            # Expect 1000x80x40 but might be less
            cls_boxes = output_boxes.tensor.detach().reshape(-1,80,4)
            max_conf = torch.zeros((cls_boxes.shape[0]))
            for cls_ind in range(0, cls_prob.shape[1]-1):
                cls_scores = cls_prob[:, cls_ind+1]
                det_boxes = cls_boxes[:,cls_ind,:]
                keep = np.array(nms(det_boxes, cls_scores, test_nms_thresh).cpu())
                max_conf[keep] = torch.where(cls_scores[keep].cpu() > max_conf[keep].cpu(), cls_scores[keep].cpu(), max_conf[keep].cpu())
            keep_boxes = torch.where(max_conf >= test_score_thresh)[0]
            
            # Limit total number of pboxes, to the best few proposals and limit the sequence length. set min and max
            if len(keep_boxes) < self.min_boxes:
                keep_boxes = np.argsort(max_conf).numpy()[::-1][:self.min_boxes]
            elif len(keep_boxes) > self.max_boxes:
                keep_boxes = np.argsort(max_conf).numpy()[::-1][:self.max_boxes]

            # TODO: Return the object labels and confidence. Currently not working
            # objects = np.argmax(cls_prob[keep_boxes.copy()][:, :-1].to("cpu"), axis=1)
            # objects_conf = np.max(cls_prob[keep_boxes.copy()][:, :-1].to("cpu"), axis=1)

            # keep_boxes is idx of >nms. output_boxes is all boxes (80x4 for each feature??) sorted by objectness confidence.
            return keep_boxes, cls_boxes#, objects, objects_conf
        # keep_boxes,objects,objects_conf = zip(*[get_output_boxes(boxes[i], batched_inputs[i], proposals[i].image_size, scores[i]) for i in range(len(proposals))])        
        keep_boxes,output_boxes = zip(*[get_output_boxes(boxes[i], batched_inputs[i], proposals[i].image_size, scores[i]) 
                                        for i in range(len(proposals))])
        
        # visual_embeds,output_boxes = zip(*[(base64.b64encode(box_feature[keep_box.copy()].detach().cpu().numpy()),
        #                                     base64.b64encode(output_box[keep_box.copy()].detach().cpu().numpy())) # TODO: boxes should be 36x4, not 36x80x4
        visual_embeds,output_boxes = zip(*[(box_feature[keep_box.copy()],
                                            output_box[keep_box.copy()]) # TODO: boxes should be 36x4, not 36x80x4
                         for box_feature, keep_box, output_box 
                         in zip(box_features,keep_boxes, output_boxes)])

        # output_boxes.shape is 36,80,4 (e.g. 80 boxes per feature, and 80=#classes),
        #  sorted by confidence. So pick the top 1 (?...)
        output_boxes = [ob[:,0,:] for ob in output_boxes]

        return visual_embeds, output_boxes, len(keep_boxes[0]) #, objects, objects_conf

    def visualise_features(self, samples):
        """Takes a sample input (generated from calling PrepareImageInputs)
        and displays the first image in the batch, with its p2,3,4,5,6 features
        and shapes"""
        images = samples[0]
        features = self.model.backbone(images.tensor)

        sample_img = samples[1][0]['image']

        fig, ax =  plt.subplots(nrows=1,ncols=6)
        ax[0].imshow(cv2.resize(np.moveaxis(sample_img.cpu().numpy(), 0,-1)[:,:,::-1]/255., (images.tensor.shape[-2:][::-1])))  
        ax[0].set_title(str(images.tensor.shape[-2:][::-1]).split('[')[1][:-2], fontsize='x-small')
        ax[0].axis('off')  
        # plt.imshow(cv2.resize(np.moveaxis(sample_img.cpu().numpy(), 0,-1)[:,:,::-1]/255., (images.tensor.shape[-2:][::-1])))
        # plt.show()
        for i,key in enumerate(features.keys()): # p2,p3,p4,p5,p6
            ax[i+1].imshow(features[key][0,0,:,:].squeeze().detach().cpu().numpy(), cmap='jet')
            ax[i+1].set_title(str(features[key].shape).split('[')[1][:-2], fontsize='x-small')
            ax[i+1].axis('off')  
            # plt.imshow(features[key][1,0,:,:].squeeze().detach().cpu().numpy(), cmap='jet')
        plt.tight_layout()
        # plt.axis('off')    
        plt.show()    

    def show_sample(self, samples):
        # Pass in prepared samples (batched_inputs)
        # slice first image
        outputs = self.model(samples[1])[0]
        # (xmin,ymin,xmax,ymax)
        # print(outputs['instances'].pred_boxes)
        v = Visualizer(samples[1][0]['image'].cpu().permute((1,2,0)), scale=1.2)
        
        outputs['instances'].pred_boxes.tensor = outputs['instances'].pred_boxes.tensor.detach()
        outputs['instances'].scores = outputs['instances'].scores.detach()
        outputs['instances'].pred_classes = outputs['instances'].pred_classes.detach()
        try:
            outputs['instances'].pred_masks = outputs['instances'].pred_masks.detach()
        except:
            # Not a mask model
            pass
        out = v.draw_instance_predictions(outputs["instances"].to("cpu"))
        cv2.imshow('',out.get_image()) #[:,:,::-1]

class PrepareImageInputs(object):
    """Convert an image to a model input
    The detectron uses resizing and normalization based on
    the configuration parameters and the input is to be provided using ImageList. 
    The model.backbone.size_divisibility handles the sizes (padding) such 
    that the FPN lateral and output convolutional features have same dimensions.
    """
    def __init__(self, extractor):
        # model cfg for resize config. 
        self.cfg = extractor.cfg
        self.size_divisibility = extractor.model.backbone.size_divisibility

        self.transform_gen = T.ResizeShortestEdge(
            [self.cfg.INPUT.MIN_SIZE_TEST, self.cfg.INPUT.MIN_SIZE_TEST], self.cfg.INPUT.MAX_SIZE_TEST
        )
        self.device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

    def __call__(self, batch):
        # detectron expects BGR images.

        img_list = [self.transform_gen.get_transform(img).apply_image(img)
                    for img in batch['images']] 
         # Convert to C,H,W format 
        convert_to_tensor = lambda x: torch.Tensor(x.astype("float32").transpose(2, 0, 1)).to(self.device)

        batched_inputs = [{"image":convert_to_tensor(img), "height": img.shape[0], "width": img.shape[1]} for img in img_list]

        # Normalizing the image
        num_channels = len(self.cfg.MODEL.PIXEL_MEAN)
        pixel_mean = torch.Tensor(self.cfg.MODEL.PIXEL_MEAN).view(num_channels, 1, 1).to(self.device)
        pixel_std = torch.Tensor(self.cfg.MODEL.PIXEL_STD).view(num_channels, 1, 1).to(self.device)
        normalizer = lambda x: (x - pixel_mean) / pixel_std
        images = [normalizer(x["image"]) for x in batched_inputs]

        # Convert to ImageList
        images =  ImageList.from_tensors(images,self.size_divisibility)
        
        return images, batched_inputs
        
        
    
    def load_mimic(self, img_path, caption_path):
        """Load and store images in a list
        detectron expects in format bgr
        caption_path: path to .json with image_id corresponding to file name
                      and caption corresponding to text report
        """

        # with open(caption_path) as f:
        #     a = json.load(f)
        # image_id = a['image_id']

        # # for im in img_path:
        #     img = 

class FeatureWriterTSV(object):
    def __init__(self, fname):
        ## full fieldnames as per butd
        # self.fieldnames = ["img_id", "img_h", "img_w", "objects_id", "objects_conf",
        #       "attrs_id", "attrs_conf", "num_boxes", "boxes", "features"]    
        self.fieldnames = ["img_id", "img_h", "img_w", 
                           "num_boxes", "boxes", "features"]
        self.fname = fname


    def __call__(self, items_dict):
        """items_dict contains list of dicts (each an image)
         with keys as per self.fieldnames"""
        
        # open in append mode for batch writing- 
        # make sure new file name for each dataset
        with open(self.fname, 'a+') as tsv:
            writer = csv.DictWriter(tsv, fieldnames=self.fieldnames, delimiter='\t')
            for item in items_dict:
                writer.writerow(item)

def load_tsv(fname, topk=None):
    """Load object features from tsv file.

    :param fname: The path to the tsv file.
    :param topk: Only load features for top K images (lines) in the tsv file.
        Will load all the features if topk is either -1 or None.
    :return: A dict of image object features where each feature is a dict.
    """
    import sys
    csv.field_size_limit(sys.maxsize)
    start_time = time.time()
    print(f"\nStarting to load pre-extracted Faster-RCNN detected objects from {fname}\n")
    with open(fname, 'r') as f:
        reader = csv.DictReader(f, ["img_id", "img_h", "img_w", 
                        "num_boxes", "boxes", "features"], delimiter="\t")
        
        data = {}
        for _, item in enumerate(reader):
            new_item = {}
            num_boxes = int(item['num_boxes'])
            for key in ['img_h', 'img_w', 'num_boxes']:
                new_item[key] = int(item[key])
            # slice from 2: to remove b' (csv.writer wraps all vals in str())
            new_item['features'] = np.frombuffer(base64.b64decode(item['features'][2:]), dtype=np.float32).reshape(num_boxes,-1).copy()
            new_item['boxes'] = np.frombuffer(base64.b64decode(item['boxes'][2:]), dtype=np.float32).reshape(num_boxes,4).copy()
            
            data[item['img_id']] = new_item
            if topk is not None and len(data) == topk:
                break
    elapsed_time = time.time() - start_time
    print(f"\n\nLoaded {len(data)} image features from {fname} in {elapsed_time:.2f} seconds.\n\n")
    return data


# def plot_sample_coco(coco_annot_path, coco_img_path):
#     coco = RawCocoDataset(COCO_ANNOT_PATH, COCO_IMG_PATH)

#     fig, ax = plt.subplots(1,4)

#     for i in range(4):
#         sample = coco[i]

#         print(i, sample['image'].shape, len(sample['captions']))
#         ax[i].imshow(sample['image'])
#         ax[i].set_title(f"sample #{i}")
#         print(sample['captions'])
#     plt.tight_layout
#     plt.show()

# def tsvReader(fname):
#     """Sample method to read feature tsv file for PT data loading"""
#     import sys
#     csv.field_size_limit(sys.maxsize)
#     with open(fname, 'r') as f:
#         reader = csv.DictReader(f, ["img_id", "img_h", "img_w", 
#                         "num_boxes", "boxes", "features"], delimiter="\t")
#         for i, item in enumerate(reader):
#             num_boxes = int(item['num_boxes'])
#             for key in ['img_h', 'img_w', 'num_boxes']:
#                 item[key] = int(item[key])
#             # slice from 2: to remove b' (csv.writer wraps all vals in str())
#             item['features'] = np.frombuffer(base64.b64decode(item['features'][2:]), dtype=np.float32).reshape(num_boxes,-1)
#             item['boxes'] = np.frombuffer(base64.b64decode(item['boxes'][2:]), dtype=np.float32).reshape(num_boxes,4) # will throw error atm
