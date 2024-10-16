import torch
from torch.functional import F
import os
import numpy as np
import json
import random

from tqdm import tqdm
from contextlib import nullcontext

from .load_model import load_model

import comfy.model_management as mm
from comfy.utils import ProgressBar, common_upscale
import folder_paths

script_directory = os.path.dirname(os.path.abspath(__file__))

class DownloadAndLoadSAM2Model:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {
            "model": ([ 
                    'sam2_hiera_base_plus.safetensors',
                    'sam2_hiera_large.safetensors',
                    'sam2_hiera_small.safetensors',
                    'sam2_hiera_tiny.safetensors',
                    'sam2.1_hiera_base_plus.safetensors',
                    'sam2.1_hiera_large.safetensors',
                    'sam2.1_hiera_small.safetensors',
                    'sam2.1_hiera_tiny.safetensors',
                    ],),
            "segmentor": (
                    ['single_image','video', 'automaskgenerator'],
                    ),
            "device": (['cuda', 'cpu', 'mps'], ),
            "precision": ([ 'fp16','bf16','fp32'],
                    {
                    "default": 'fp16'
                    }),

            },
        }

    RETURN_TYPES = ("SAM2MODEL",)
    RETURN_NAMES = ("sam2_model",)
    FUNCTION = "loadmodel"
    CATEGORY = "SAM2"

    def loadmodel(self, model, segmentor, device, precision):
        if precision != 'fp32' and device == 'cpu':
            raise ValueError("fp16 and bf16 are not supported on cpu")

        if device == "cuda":
            if torch.cuda.get_device_properties(0).major >= 8:
                # turn on tfloat32 for Ampere GPUs (https://pytorch.org/docs/stable/notes/cuda.html#tensorfloat-32-tf32-on-ampere-devices)
                torch.backends.cuda.matmul.allow_tf32 = True
                torch.backends.cudnn.allow_tf32 = True
        dtype = {"bf16": torch.bfloat16, "fp16": torch.float16, "fp32": torch.float32}[precision]
        device = {"cuda": torch.device("cuda"), "cpu": torch.device("cpu"), "mps": torch.device("mps")}[device]

        download_path = os.path.join(folder_paths.models_dir, "sam2")
        if precision != 'fp32' and "2.1" in model:
            base_name, extension = model.rsplit('.', 1)
            model = f"{base_name}-fp16.{extension}"
        model_path = os.path.join(download_path, model)
        print("model_path: ", model_path)
        
        if not os.path.exists(model_path):
            print(f"Downloading SAM2 model to: {model_path}")
            from huggingface_hub import snapshot_download
            snapshot_download(repo_id="Kijai/sam2-safetensors",
                            allow_patterns=[f"*{model}*"],
                            local_dir=download_path,
                            local_dir_use_symlinks=False)

        model_mapping = {
            "2.0": {
                "base": "sam2_hiera_b+.yaml",
                "large": "sam2_hiera_l.yaml",
                "small": "sam2_hiera_s.yaml",
                "tiny": "sam2_hiera_t.yaml"
            },
            "2.1": {
                "base": "sam2.1_hiera_b+.yaml",
                "large": "sam2.1_hiera_l.yaml",
                "small": "sam2.1_hiera_s.yaml",
                "tiny": "sam2.1_hiera_t.yaml"
            }
        }
        version = "2.1" if "2.1" in model else "2.0"

        model_cfg_path = next(
            (os.path.join(script_directory, "sam2_configs", cfg) 
            for key, cfg in model_mapping[version].items() if key in model),
            None
        )
        print(f"Using model config: {model_cfg_path}")

        model = load_model(model_path, model_cfg_path, segmentor, dtype, device)
        
        sam2_model = {
            'model': model, 
            'dtype': dtype,
            'device': device,
            'segmentor' : segmentor,
            'version': version
            }

        return (sam2_model,)


class Florence2toCoordinates:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "data": ("JSON", ),
                "index": ("STRING", {"default": "0"}),
                "batch": ("BOOLEAN", {"default": False}),
            },
            
        }
    
    RETURN_TYPES = ("STRING", "BBOX")
    RETURN_NAMES =("center_coordinates", "bboxes")
    FUNCTION = "segment"
    CATEGORY = "SAM2"

    def segment(self, data, index, batch=False):
        print(data)
        try:
            coordinates = coordinates.replace("'", '"')
            coordinates = json.loads(coordinates)
        except:
            coordinates = data
        print("Type of data:", type(data))
        print("Data:", data)
        if len(data)==0:
            return (json.dumps([{'x': 0, 'y': 0}]),)
        center_points = []

        if index.strip():  # Check if index is not empty
            indexes = [int(i) for i in index.split(",")]
        else:  # If index is empty, use all indices from data[0]
            indexes = list(range(len(data[0])))

        print("Indexes:", indexes)
        bboxes = []
        
        if batch:
            for idx in indexes:
                if 0 <= idx < len(data[0]):
                    for i in range(len(data)):
                        bbox = data[i][idx]
                        min_x, min_y, max_x, max_y = bbox
                        center_x = int((min_x + max_x) / 2)
                        center_y = int((min_y + max_y) / 2)
                        center_points.append({"x": center_x, "y": center_y})
                        bboxes.append(bbox)
        else:
            for idx in indexes:
                if 0 <= idx < len(data[0]):
                    bbox = data[0][idx]
                    min_x, min_y, max_x, max_y = bbox
                    center_x = int((min_x + max_x) / 2)
                    center_y = int((min_y + max_y) / 2)
                    center_points.append({"x": center_x, "y": center_y})
                    bboxes.append(bbox)
                else:
                    raise ValueError(f"There's nothing in index: {idx}")
                
        coordinates = json.dumps(center_points)
        print("Coordinates:", coordinates)
        return (coordinates, bboxes)
    
class Sam2Segmentation:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "sam2_model": ("SAM2MODEL", ),
                "image": ("IMAGE", ),
                "keep_model_loaded": ("BOOLEAN", {"default": True}),
            },
            "optional": {
                "coordinates_positive": ("STRING", {"forceInput": True}),
                "coordinates_negative": ("STRING", {"forceInput": True}),
                "bboxes": ("BBOX", ),
                "individual_objects": ("BOOLEAN", {"default": False}),
                "mask": ("MASK", ),
                "enabled": ("BOOLEAN", {"default": True, "label_on": "enabled", "label_off": "disabled"}),
            },
        }
    
    RETURN_TYPES = ("MASK", )
    RETURN_NAMES =("mask", )
    FUNCTION = "segment"
    CATEGORY = "SAM2"

    def segment(self, image, sam2_model, keep_model_loaded, coordinates_positive=None, coordinates_negative=None, 
                individual_objects=False, bboxes=None, mask=None, enabled=True):
        if not enabled:
            mask_tensor = torch.zeros(image.shape[1:-1]).unsqueeze(0)
            return (mask_tensor,)
        
        offload_device = mm.unet_offload_device()
        model = sam2_model["model"]
        device = sam2_model["device"]
        dtype = sam2_model["dtype"]
        segmentor = sam2_model["segmentor"]
        B, H, W, C = image.shape
        
        if mask is not None:
            input_mask = mask.clone().unsqueeze(1)
            input_mask = F.interpolate(input_mask, size=(256, 256), mode="bilinear")
            input_mask = input_mask.squeeze(1)

        if segmentor == 'automaskgenerator':
            raise ValueError("For automaskgenerator use Sam2AutoMaskSegmentation -node")
        if segmentor == 'single_image' and B > 1:
            print("Segmenting batch of images with single_image segmentor")

        if segmentor == 'video' and bboxes is not None and "2.1" not in sam2_model["version"]:
            raise ValueError("2.0 model doesn't support bboxes with video segmentor")

        if segmentor == 'video': # video model needs images resized first thing
            model_input_image_size = model.image_size
            print("Resizing to model input image size: ", model_input_image_size)
            image = common_upscale(image.movedim(-1,1), model_input_image_size, model_input_image_size, "bilinear", "disabled").movedim(1,-1)

        #handle point coordinates
        if coordinates_positive is not None:
            try:
                coordinates_positive = json.loads(coordinates_positive.replace("'", '"'))
                coordinates_positive = [(coord['x'], coord['y']) for coord in coordinates_positive]
                if coordinates_negative is not None:
                    coordinates_negative = json.loads(coordinates_negative.replace("'", '"'))
                    coordinates_negative = [(coord['x'], coord['y']) for coord in coordinates_negative]
            except:
                pass
            
            if not individual_objects:
                positive_point_coords = np.atleast_2d(np.array(coordinates_positive))
            else:
                positive_point_coords = np.array([np.atleast_2d(coord) for coord in coordinates_positive])

            if coordinates_negative is not None:
                negative_point_coords = np.array(coordinates_negative)
                # Ensure both positive and negative coords are lists of 2D arrays if individual_objects is True
                if individual_objects:
                    assert negative_point_coords.shape[0] <= positive_point_coords.shape[0], "Can't have more negative than positive points in individual_objects mode"
                    if negative_point_coords.ndim == 2:
                        negative_point_coords = negative_point_coords[:, np.newaxis, :]
                    # Extend negative coordinates to match the number of positive coordinates
                    while negative_point_coords.shape[0] < positive_point_coords.shape[0]:
                        negative_point_coords = np.concatenate((negative_point_coords, negative_point_coords[:1, :, :]), axis=0)
                    final_coords = np.concatenate((positive_point_coords, negative_point_coords), axis=1)
                else:
                    final_coords = np.concatenate((positive_point_coords, negative_point_coords), axis=0)
            else:
                final_coords = positive_point_coords

        # Handle possible bboxes
        if bboxes is not None:
            boxes_np_batch = []
            for bbox_list in bboxes:
                boxes_np = []
                for bbox in bbox_list:
                    boxes_np.append(bbox)
                boxes_np = np.array(boxes_np)
                boxes_np_batch.append(boxes_np)
            if individual_objects:
                final_box = np.array(boxes_np_batch)
            else:
                final_box = np.array(boxes_np)
            final_labels = None

        #handle labels
        if coordinates_positive is not None:
            if not individual_objects:
                positive_point_labels = np.ones(len(positive_point_coords))
            else:
                positive_labels = []
                for point in positive_point_coords:
                    positive_labels.append(np.array([1])) # 1)
                positive_point_labels = np.stack(positive_labels, axis=0)
                
            if coordinates_negative is not None:
                if not individual_objects:
                    negative_point_labels = np.zeros(len(negative_point_coords))  # 0 = negative
                    final_labels = np.concatenate((positive_point_labels, negative_point_labels), axis=0)
                else:
                    negative_labels = []
                    for point in positive_point_coords:
                        negative_labels.append(np.array([0])) # 1)
                    negative_point_labels = np.stack(negative_labels, axis=0)
                    #combine labels
                    final_labels = np.concatenate((positive_point_labels, negative_point_labels), axis=1)                    
            else:
                final_labels = positive_point_labels
            print("combined labels: ", final_labels)
            print("combined labels shape: ", final_labels.shape)          
        
        mask_list = []
        try:
            model.to(device)
        except:
            model.model.to(device)
        
        autocast_condition = not mm.is_device_mps(device)
        with torch.autocast(mm.get_autocast_device(device), dtype=dtype) if autocast_condition else nullcontext():
            if segmentor == 'single_image':
                image_np = (image.contiguous() * 255).byte().numpy()
                comfy_pbar = ProgressBar(len(image_np))
                tqdm_pbar = tqdm(total=len(image_np), desc="Processing Images")
                for i in range(len(image_np)):
                    model.set_image(image_np[i])
                    if bboxes is None:
                        input_box = None
                    else:
                        if len(image_np) > 1:
                            input_box = final_box[i]
                        input_box = final_box
                    
                    out_masks, scores, logits = model.predict(
                        point_coords=final_coords if coordinates_positive is not None else None, 
                        point_labels=final_labels if coordinates_positive is not None else None,
                        box=input_box,
                        multimask_output=True if not individual_objects else False,
                        mask_input = input_mask[i].unsqueeze(0) if mask is not None else None,
                        )
                
                    if out_masks.ndim == 3:
                        sorted_ind = np.argsort(scores)[::-1]
                        out_masks = out_masks[sorted_ind][0] #choose only the best result for now
                        scores = scores[sorted_ind]
                        logits = logits[sorted_ind]
                        mask_list.append(np.expand_dims(out_masks, axis=0))
                    else:
                        _, _, H, W = out_masks.shape
                        # Combine masks for all object IDs in the frame
                        combined_mask = np.zeros((H, W), dtype=bool)
                        for out_mask in out_masks:
                            combined_mask = np.logical_or(combined_mask, out_mask)
                        combined_mask = combined_mask.astype(np.uint8)
                        mask_list.append(combined_mask)
                    comfy_pbar.update(1)
                    tqdm_pbar.update(1)

            elif segmentor == 'video':
                mask_list = []
                if hasattr(self, 'inference_state'):
                    model.reset_state(self.inference_state)
                self.inference_state = model.init_state(image.permute(0, 3, 1, 2).contiguous(), H, W, device=device)
                if bboxes is None:
                        input_box = None
                else:
                    input_box = bboxes[0]
                
                if individual_objects and bboxes is not None:
                    raise ValueError("bboxes not supported with individual_objects")


                if individual_objects:
                    for i, (coord, label) in enumerate(zip(final_coords, final_labels)):
                        _, out_obj_ids, out_mask_logits = model.add_new_points_or_box(
                        inference_state=self.inference_state,
                        frame_idx=0,
                        obj_id=i,
                        points=final_coords[i],
                        labels=final_labels[i],
                        clear_old_points=True,
                        box=input_box
                        )
                else:
                    _, out_obj_ids, out_mask_logits = model.add_new_points_or_box(
                        inference_state=self.inference_state,
                        frame_idx=0,
                        obj_id=1,
                        points=final_coords if coordinates_positive is not None else None, 
                        labels=final_labels if coordinates_positive is not None else None,
                        clear_old_points=True,
                        box=input_box
                    )

                pbar = ProgressBar(B)
                video_segments = {}
                for out_frame_idx, out_obj_ids, out_mask_logits in model.propagate_in_video(self.inference_state):
                    video_segments[out_frame_idx] = {
                        out_obj_id: (out_mask_logits[i] > 0.0).cpu().numpy()
                        for i, out_obj_id in enumerate(out_obj_ids)
                        }
                    pbar.update(1)
                    if individual_objects:
                        _, _, H, W = out_mask_logits.shape
                        # Combine masks for all object IDs in the frame
                        combined_mask = np.zeros((H, W), dtype=np.uint8) 
                        for i, out_obj_id in enumerate(out_obj_ids):
                            out_mask = (out_mask_logits[i] > 0.0).cpu().numpy()
                            combined_mask = np.logical_or(combined_mask, out_mask)
                        video_segments[out_frame_idx] = combined_mask

                if individual_objects:
                    for frame_idx, combined_mask in video_segments.items():
                        mask_list.append(combined_mask)
                else:
                    for frame_idx, obj_masks in video_segments.items():
                        for out_obj_id, out_mask in obj_masks.items():
                            mask_list.append(out_mask)

        if not keep_model_loaded:
            try:
                model.to(offload_device)
            except:
                model.model.to(offload_device)
        
        out_list = []
        for mask in mask_list:
            mask_tensor = torch.from_numpy(mask)
            mask_tensor = mask_tensor.permute(1, 2, 0)
            mask_tensor = mask_tensor[:, :, 0]
            out_list.append(mask_tensor)
        mask_tensor = torch.stack(out_list, dim=0).cpu().float()
        return (mask_tensor,)

class Sam2VideoSegmentationAddPoints:
    @classmethod
    def IS_CHANGED(s): # TODO: smarter reset?
        return ""
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "sam2_model": ("SAM2MODEL", ),
                "coordinates_positive": ("STRING", {"forceInput": True}),
                "frame_index": ("INT", {"default": 0}),
                "object_index": ("INT", {"default": 0}),
            },
            "optional": {
                "image": ("IMAGE", ),
                "coordinates_negative": ("STRING", {"forceInput": True}),
                "prev_inference_state": ("SAM2INFERENCESTATE", ),
            },
        }
    
    RETURN_TYPES = ("SAM2MODEL", "SAM2INFERENCESTATE", )
    RETURN_NAMES =("sam2_model", "inference_state", )
    FUNCTION = "segment"
    CATEGORY = "SAM2"

    def segment(self, sam2_model, coordinates_positive, frame_index, object_index, image=None, coordinates_negative=None, prev_inference_state=None):
        offload_device = mm.unet_offload_device()
        model = sam2_model["model"]
        device = sam2_model["device"]
        dtype = sam2_model["dtype"]
        segmentor = sam2_model["segmentor"]
        

        if segmentor != 'video':
            raise ValueError("Loaded model is not SAM2Video")
        if image is not None:
            B, H, W, C = image.shape
            model_input_image_size = model.image_size
            print("Resizing to model input image size: ", model_input_image_size)
            image = common_upscale(image.movedim(-1,1), model_input_image_size, model_input_image_size, "bilinear", "disabled").movedim(1,-1)

        try:
            coordinates_positive = json.loads(coordinates_positive.replace("'", '"'))
            coordinates_positive = [(coord['x'], coord['y']) for coord in coordinates_positive]
            if coordinates_negative is not None:
                coordinates_negative = json.loads(coordinates_negative.replace("'", '"'))
                coordinates_negative = [(coord['x'], coord['y']) for coord in coordinates_negative]
        except:
            pass
        
        positive_point_coords = np.array(coordinates_positive)
        positive_point_labels = [1] * len(positive_point_coords)  # 1 = positive
        positive_point_labels = np.array(positive_point_labels)
        print("positive coordinates: ", positive_point_coords)

        if coordinates_negative is not None:
            negative_point_coords = np.array(coordinates_negative)
            negative_point_labels = [0] * len(negative_point_coords)  # 0 = negative
            negative_point_labels = np.array(negative_point_labels)
            print("negative coordinates: ", negative_point_coords)

            # Combine coordinates and labels
        else:
            negative_point_coords = np.empty((0, 2))
            negative_point_labels = np.array([])
        # Ensure both positive and negative coordinates are 2D arrays
        positive_point_coords = np.atleast_2d(positive_point_coords)
        negative_point_coords = np.atleast_2d(negative_point_coords)

        # Ensure both positive and negative labels are 1D arrays
        positive_point_labels = np.atleast_1d(positive_point_labels)
        negative_point_labels = np.atleast_1d(negative_point_labels)

        combined_coords = np.concatenate((positive_point_coords, negative_point_coords), axis=0)
        combined_labels = np.concatenate((positive_point_labels, negative_point_labels), axis=0)
        
        model.to(device)
        
        autocast_condition = not mm.is_device_mps(device)
        with torch.autocast(mm.get_autocast_device(model.device), dtype=dtype) if autocast_condition else nullcontext(): 
            if prev_inference_state is None:
                print("Initializing inference state")
                if hasattr(self, 'inference_state'):
                    model.reset_state(self.inference_state)
                self.inference_state = model.init_state(image.permute(0, 3, 1, 2).contiguous(), H, W, device=device)
            else:
                print("Using previous inference state")
                B = prev_inference_state['num_frames']
                self.inference_state = prev_inference_state['inference_state']
            _, out_obj_ids, out_mask_logits = model.add_new_points(
                inference_state=self.inference_state,
                frame_idx=frame_index,
                obj_id=object_index,
                points=combined_coords,
                labels=combined_labels,
            )
        inference_state = {
            "inference_state": self.inference_state,
            "num_frames": B,
        }
        sam2_model = {
            'model': model, 
            'dtype': dtype,
            'device': device,
            'segmentor' : segmentor
            }    
        return (sam2_model, inference_state,)

class Sam2VideoSegmentation:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "sam2_model": ("SAM2MODEL", ),
                "inference_state": ("SAM2INFERENCESTATE", ),
                "keep_model_loaded": ("BOOLEAN", {"default": True}),
            },
        }
    
    RETURN_TYPES = ("MASK", )
    RETURN_NAMES =("mask", )
    FUNCTION = "segment"
    CATEGORY = "SAM2"

    def segment(self, sam2_model, inference_state, keep_model_loaded):
        offload_device = mm.unet_offload_device()
        model = sam2_model["model"]
        device = sam2_model["device"]
        dtype = sam2_model["dtype"]
        segmentor = sam2_model["segmentor"]
        inference_state = inference_state["inference_state"]
        B = inference_state["num_frames"]

        if segmentor != 'video':
            raise ValueError("Loaded model is not SAM2Video")

        model.to(device)
        
        autocast_condition = not mm.is_device_mps(device)
        with torch.autocast(mm.get_autocast_device(device), dtype=dtype) if autocast_condition else nullcontext(): 
            
            #if hasattr(self, 'inference_state'):
            #    model.reset_state(self.inference_state)

            pbar = ProgressBar(B)
            video_segments = {}
            for out_frame_idx, out_obj_ids, out_mask_logits in model.propagate_in_video(inference_state):
                print("out_mask_logits",out_mask_logits.shape)
                _, _, H, W = out_mask_logits.shape
                # Combine masks for all object IDs in the frame
                combined_mask = np.zeros((H, W), dtype=np.uint8) 
                for i, out_obj_id in enumerate(out_obj_ids):
                    out_mask = (out_mask_logits[i] > 0.0).cpu().numpy()
                    combined_mask = np.logical_or(combined_mask, out_mask)
                video_segments[out_frame_idx] = combined_mask
                pbar.update(1)

            mask_list = []
            # Collect the combined masks
            for frame_idx, combined_mask in video_segments.items():
                mask_list.append(combined_mask)
            print(f"Total masks collected: {len(mask_list)}")

        if not keep_model_loaded:
            model.to(offload_device)
        
        out_list = []
        for mask in mask_list:
            mask_tensor = torch.from_numpy(mask)
            mask_tensor = mask_tensor.permute(1, 2, 0)
            mask_tensor = mask_tensor[:, :, 0]
            out_list.append(mask_tensor)
        mask_tensor = torch.stack(out_list, dim=0).cpu().float()
        return (mask_tensor,)
        
class Sam2AutoSegmentation:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "sam2_model": ("SAM2MODEL", ),
                "image": ("IMAGE", ),
                "points_per_side": ("INT", {"default": 32}),
                "points_per_batch": ("INT", {"default": 64}),
                "pred_iou_thresh": ("FLOAT", {"default": 0.8, "min": 0.0, "max": 1.0, "step": 0.01}),
                "stability_score_thresh": ("FLOAT", {"default": 0.95, "min": 0.0, "max": 1.0, "step": 0.01}),
                "stability_score_offset": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 1.0, "step": 0.01}),
                "mask_threshold": ("FLOAT", {"default": 0.0, "min": 0.0, "max": 1.0, "step": 0.01}),
                "crop_n_layers": ("INT", {"default": 0}),
                "box_nms_thresh": ("FLOAT", {"default": 0.7, "min": 0.0, "max": 1.0, "step": 0.01}),
                "crop_nms_thresh": ("FLOAT", {"default": 0.7, "min": 0.0, "max": 1.0, "step": 0.01}),
                "crop_overlap_ratio": ("FLOAT", {"default": 0.34, "min": 0.0, "max": 1.0, "step": 0.01}),
                "crop_n_points_downscale_factor": ("INT", {"default": 1}),
                "min_mask_region_area": ("FLOAT", {"default": 0.0, "min": 0.0, "max": 1.0, "step": 0.01}),
                "use_m2m": ("BOOLEAN", {"default": False}),
                "keep_model_loaded": ("BOOLEAN", {"default": True}),
            },
        }
    
    RETURN_TYPES = ("MASK", "IMAGE", "BBOX",)
    RETURN_NAMES =("mask", "segmented_image", "bbox" ,)
    FUNCTION = "segment"
    CATEGORY = "SAM2"

    def segment(self, image, sam2_model, points_per_side, points_per_batch, pred_iou_thresh, stability_score_thresh, 
                stability_score_offset, crop_n_layers, box_nms_thresh, crop_n_points_downscale_factor, min_mask_region_area, 
                use_m2m, mask_threshold, crop_nms_thresh, crop_overlap_ratio, keep_model_loaded):
        offload_device = mm.unet_offload_device()
        model = sam2_model["model"]
        device = sam2_model["device"]
        dtype = sam2_model["dtype"]
        segmentor = sam2_model["segmentor"]
        
        if segmentor != 'automaskgenerator':
            raise ValueError("Loaded model is not SAM2AutomaticMaskGenerator")
        
        model.points_per_side=points_per_side
        model.points_per_batch=points_per_batch
        model.pred_iou_thresh=pred_iou_thresh
        model.stability_score_thresh=stability_score_thresh
        model.stability_score_offset=stability_score_offset
        model.crop_n_layers=crop_n_layers
        model.box_nms_thresh=box_nms_thresh
        model.crop_n_points_downscale_factor=crop_n_points_downscale_factor
        model.crop_nms_thresh=crop_nms_thresh
        model.crop_overlap_ratio=crop_overlap_ratio
        model.min_mask_region_area=min_mask_region_area
        model.use_m2m=use_m2m
        model.mask_threshold=mask_threshold
        
        model.predictor.model.to(device)
        
        B, H, W, C = image.shape
        image_np = (image.contiguous() * 255).byte().numpy()

        out_list = []
        segment_out_list = []
        mask_list=[]
        
        pbar = ProgressBar(B)
        autocast_condition = not mm.is_device_mps(device)
        with torch.autocast(mm.get_autocast_device(device), dtype=dtype) if autocast_condition else nullcontext():
            for img_np in image_np:
                result_dict = model.generate(img_np)
                mask_list = [item['segmentation'] for item in result_dict]
                bbox_list = [item['bbox'] for item in result_dict]

                # Generate random colors for each mask
                num_masks = len(mask_list)
                colors = [tuple(random.choices(range(256), k=3)) for _ in range(num_masks)]
                
                # Create a blank image to overlay masks
                overlay_image = np.zeros((H, W, 3), dtype=np.uint8)

                # Create a combined mask initialized to zeros
                combined_mask = np.zeros((H, W), dtype=np.uint8)

                # Iterate through masks and color them
                for mask, color in zip(mask_list, colors):

                    # Combine masks using logical OR
                    combined_mask = np.logical_or(combined_mask, mask).astype(np.uint8)
                    
                    # Convert mask to numpy array
                    mask_np = mask.astype(np.uint8)
                    
                    # Color the mask
                    colored_mask = np.zeros_like(overlay_image)
                    for i in range(3):  # Apply color channel-wise
                        colored_mask[:, :, i] = mask_np * color[i]
                    
                    # Blend the colored mask with the overlay image
                    overlay_image = np.where(colored_mask > 0, colored_mask, overlay_image)
                out_list.append(torch.from_numpy(combined_mask))
                segment_out_list.append(overlay_image)
                pbar.update(1)

        stacked_array = np.stack(segment_out_list, axis=0)
        segment_image_tensor = torch.from_numpy(stacked_array).float() / 255

        if not keep_model_loaded:
           model.predictor.model.to(offload_device)
        
        mask_tensor = torch.stack(out_list, dim=0)
        return (mask_tensor.cpu().float(), segment_image_tensor.cpu().float(), bbox_list)

#WIP    
# class OwlV2Detector:
#     @classmethod
#     def INPUT_TYPES(s):
#         return {
#             "required": {
#                 "image": ("IMAGE", ),
#             },
#         }
    
#     RETURN_TYPES = ("MASK", )
#     RETURN_NAMES =("mask", )
#     FUNCTION = "segment"
#     CATEGORY = "SAM2"

#     def segment(self, image):
#         from transformers import Owlv2Processor, Owlv2ForObjectDetection
#         device = mm.get_torch_device()
#         offload_device = mm.unet_offload_device()
#         processor = Owlv2Processor.from_pretrained("google/owlv2-base-patch16-ensemble")
#         model = Owlv2ForObjectDetection.from_pretrained("google/owlv2-base-patch16-ensemble")

#         url = "http://images.cocodataset.org/val2017/000000039769.jpg"
#         image = Image.open(requests.get(url, stream=True).raw)
#         texts = [["a photo of a cat", "a photo of a dog"]]
#         inputs = processor(text=texts, images=image, return_tensors="pt")
#         outputs = model(**inputs)

#         # Target image sizes (height, width) to rescale box predictions [batch_size, 2]
#         target_sizes = torch.Tensor([image.size[::-1]])
#         # Convert outputs (bounding boxes and class logits) to Pascal VOC Format (xmin, ymin, xmax, ymax)
#         results = processor.post_process_object_detection(outputs=outputs, target_sizes=target_sizes, threshold=0.1)
#         i = 0  # Retrieve predictions for the first image for the corresponding text queries
#         text = texts[i]
#         boxes, scores, labels = results[i]["boxes"], results[i]["scores"], results[i]["labels"]
#         for box, score, label in zip(boxes, scores, labels):
#             box = [round(i, 2) for i in box.tolist()]
#             print(f"Detected {text[label]} with confidence {round(score.item(), 3)} at location {box}")


#         return (mask_tensor,)
     
NODE_CLASS_MAPPINGS = {
    "DownloadAndLoadSAM2Model": DownloadAndLoadSAM2Model,
    "Sam2Segmentation": Sam2Segmentation,
    "Florence2toCoordinates": Florence2toCoordinates,
    "Sam2AutoSegmentation": Sam2AutoSegmentation,
    "Sam2VideoSegmentationAddPoints": Sam2VideoSegmentationAddPoints,
    "Sam2VideoSegmentation": Sam2VideoSegmentation
}
NODE_DISPLAY_NAME_MAPPINGS = {
    "DownloadAndLoadSAM2Model": "(Down)Load SAM2Model",
    "Sam2Segmentation": "Sam2Segmentation",
    "Florence2toCoordinates": "Florence2 Coordinates",
    "Sam2AutoSegmentation": "Sam2AutoSegmentation",
    "Sam2VideoSegmentationAddPoints": "Sam2VideoSegmentationAddPoints",
    "Sam2VideoSegmentation": "Sam2VideoSegmentation"
}
