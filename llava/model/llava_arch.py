from abc import ABC, abstractmethod

import math
import torch
import torch.nn as nn
from multimodal_encoder.builder import build_vision_tower
from multimodal_resampler.builder import build_vision_resampler
from multimodal_projector.builder import build_vision_projector
from constants import IMAGE_TOKEN_INDEX, IGNORE_INDEX, DEFAULT_IMAGE_PATCH_TOKEN, DEFAULT_IM_START_TOKEN, DEFAULT_IM_END_TOKEN

class LlavaMetaModel:
    def __init__(self, config):

        if hasattr(config, "mm_vision_tower"):
            self.vision_tower = build_vision_tower(config, delay_load=getattr(config, "delay_load", False))
            self.vision_resampler = build_vision_resampler(config, vision_tower=self.vision_tower)
            self.mm_projector = build_vision_projector(config, vision_cfg=self.vision_tower.config)

            if "unpad" in getattr(config, "mm_patch_merge_type", ""):
                self.image_newline = nn.Parameter(torch.empty(config.hidden_size))

    def get_vision_tower(self):
        return self.vision_tower

    def initialize_vision_modules(self, model_args):
        # Update config with vision-related parameters
        self.config.mm_vision_tower = model_args.vision_tower
        self.config.mm_hidden_size = self.vision_resampler.hidden_size
        self.config.mm_vision_select_layer = model_args.mm_vision_select_layer
        self.config.mm_vision_select_feature = model_args.mm_vision_select_feature
        self.config.mm_patch_merge_type = model_args.mm_patch_merge_type

        # Load vision tower if not already loaded
        if self.vision_tower is None:
            self.vision_tower = build_vision_tower(model_args)
            self.vision_resampler = build_vision_resampler(model_args, vision_tower=self.vision_tower)
        else:
            self.vision_tower.load_model()

        # Ensure gradients are enabled for vision resampler
        for p in self.vision_resampler.parameters():
            p.requires_grad = True

        # Initialize mm_projector if not already present
        if not hasattr(self, "mm_projector"):
            self.mm_projector = build_vision_projector(self.config, vision_cfg=self.vision_tower.config)

        # Add image_newline parameter if required :: serves as a 'separator' token between image and text (learnable token)
        if "unpad" in self.config.mm_patch_merge_type and not hasattr(self, "image_newline"):
            self.image_newline = nn.Parameter(torch.randn(self.config.hidden_size) / (self.config.hidden_size ** 0.5))

        # Ensure gradients are enabled for mm_projector
        for p in self.mm_projector.parameters():
            p.requires_grad = True
            
            
def unpad_image(tensor, original_size):
    """
    Unpads a PyTorch tensor of a padded and resized image.

    Args:
    tensor (torch.Tensor): The image tensor, assumed to be in CxHxW format.
    original_size (tuple): The original size of the image (width, height).

    Returns:
    torch.Tensor: The unpadded image tensor.
    """
    original_width, original_height = original_size
    current_height, current_width = tensor.shape[1:]
    
    padding_height = (current_height - original_height) // 2
    padding_width = (current_width - original_width) // 2

    height_start = padding_height
    width_start = padding_width
    
    # Slice the tensor
    unpadded_tensor = tensor[:, 
                             height_start : current_height - height_start,
                             width_start : current_width - width_start]

    return unpadded_tensor


class LlavaMetaForCausalLM(ABC):
    
    @abstractmethod
    def get_model(self):
        pass 
    
    def get_vision_tower(self):
        return self.get_model().get_vision_tower()
    
    def add_token_per_frame(self, image_feature):
        """ 
        Image_feature: [num_frames, num_patches, feature_dim]
        Assume square grid (grid is an ensemble of patches, so H=W=sqrt(num_patches))
        We append an 'image-end' token to each frame
        Detail: shape of image_newline: [feature_dim] -- so we operate after the multi-modal projector
        """
        image_feature = image_feature.permute(2, 0, 1).contiguous()
        expand_tokens = self.model.image_newline[:, None, None].expand(*image_feature.shape[:-1], 1).to(image_feature.device)
        image_feature = torch.cat((image_feature, expand_tokens), dim=-1) # [feature_dim, num_frames, num_patches+1]
        image_feature = image_feature.permute(1, 2, 0).contiguous() # [num_frames, num_patches+1, feature_dim]
        return image_feature 
    
    def encode_images(self, images): # Should work for list of images here (!)
        """ 
        images: [num_images, C, H, W]
        """
        image_features = self.get_model().get_vision_tower()(images)
        image_features = self.get_model().mm_projector(image_features)
        # image_features = [self.add_token_per_frame(image_feature) for image_feature in image_features]
        
        return image_features # [num_images, num_patches, feature_dim]
    
    
    def prepare_inputs_labels_for_multimodal(self, input_ids, position_ids, attention_mask, past_key_values, labels, images):
        """ 
        Interleave Image features with Text features and construct input sequence embeddings
        Images: [batch_size, num_images, C, H, W] (Assuming multiple images per sample)
        Input_ids represent image with IMAGE_TOKEN_INDEX, which is converted into num_patches tokens
        Labels, Input_ids, attention_mask are updated accordingly, padding is also applied here
        --------------------------------------------------------------------------------------------
        Error Report when number of IMAGE_TOKEN_INDEX in input_ids does not match number of images

        Wierd: Position_ids & Past_key_values never used in calculation
        """
        vision_tower = self.get_vision_tower()
        if vision_tower is None or images is None or input_ids.shape[1] == 1:
            return input_ids, position_ids, attention_mask, past_key_values, None, labels

        # Encode all images
        image_features = []
        assert len(images) == len(input_ids), f"Mismatch in batch_size of images and input_ids: {len(images)} vs {len(input_ids)}"
        batch_size = len(images)
        
        for batch_images in images:
            num_images, C, H, W = batch_images.shape
            batch_image_features = self.encode_images(batch_images)
            image_features.append(batch_image_features)

        # Process input ids and labels
        input_ids = [ids[mask] for ids, mask in zip(input_ids, attention_mask)]
        labels = [labs[mask] for labs, mask in zip(labels, attention_mask)]

        new_input_embeds = []
        new_labels = []
        for batch_idx, (cur_input_ids, cur_image_features) in enumerate(zip(input_ids, image_features)):
            num_images_in_sequence = (cur_input_ids == IMAGE_TOKEN_INDEX).sum()
            assert num_images_in_sequence == cur_image_features.shape[0], f"Mismatch in number of images in input_ids and images: {num_images_in_sequence} vs {cur_image_features.shape[0]}"

            if num_images == 0:
                cur_input_embeds = self.get_model().embed_tokens(cur_input_ids)
                new_input_embeds.append(cur_input_embeds)
                new_labels.append(labels[batch_idx])
                continue

            # Split input ids and labels at image tokens
            split_indices = [-1] + torch.where(cur_input_ids == IMAGE_TOKEN_INDEX)[0].tolist() + [cur_input_ids.shape[0]]
            cur_input_ids_chunks = [cur_input_ids[split_indices[i]+1:split_indices[i+1]] for i in range(len(split_indices)-1) if split_indices[i+1] - split_indices[i] > 1]
            cur_labels_chunks = [labels[batch_idx][split_indices[i]+1:split_indices[i+1]] for i in range(len(split_indices)-1) if split_indices[i+1] - split_indices[i] > 1]

            # Interleaved text embeddings and image features
            cur_input_embeds = []
            cur_labels = []
            for i, (ids_chunk, labels_chunk) in enumerate(zip(cur_input_ids_chunks, cur_labels_chunks)):
                cur_input_embeds.append(self.get_model().embed_tokens(ids_chunk))
                cur_labels.append(labels_chunk)
                if i < num_images:
                    cur_input_embeds.append(image_features[batch_idx, i])
                    cur_labels.append(torch.full((image_features[batch_idx, i].shape[0],), IGNORE_INDEX, device=labels_chunk.device, dtype=labels_chunk.dtype))
                        
            new_input_embeds.append(torch.cat(cur_input_embeds))
            new_labels.append(torch.cat(cur_labels))

        # Pad sequences to max length
        max_len = max(x.shape[0] for x in new_input_embeds)
        new_input_embeds_padded = []
        new_labels_padded = torch.full((batch_size, max_len), IGNORE_INDEX, dtype=new_labels[0].dtype, device=new_labels[0].device)
        attention_mask = torch.zeros((batch_size, max_len), dtype=torch.bool, device=new_labels[0].device)

        for i, (cur_embeds, cur_labels) in enumerate(zip(new_input_embeds, new_labels)):
            cur_len = cur_embeds.shape[0]
            new_input_embeds_padded.append(torch.cat((cur_embeds, torch.zeros((max_len - cur_len, cur_embeds.shape[1]), dtype=cur_embeds.dtype, device=cur_embeds.device))))
            new_labels_padded[i, :cur_len] = cur_labels
            attention_mask[i, :cur_len] = True

        new_input_embeds = torch.stack(new_input_embeds_padded)

        return None, None, attention_mask, past_key_values, new_input_embeds, new_labels_padded
        
        
    def initialize_vision_tokenizer(self, model_args, tokenizer):
        """ 
        Add special tokens for image-start, image-end, image-patch input ids
        Output embedding is not trainable since we do NOT wish to generate image
        Input embedding is the weight for token embedding matrix, output embedding is the weight for the final LM head (with softmax to predict the logits)
        
        This is useful to project special tokens into embedding space, the 'input_ids' need to convert an image into [IM_START_TOKEN, IMAGE_TOKEN_INDEX, IM_END_TOKEN] and goes through the above function
        """
        new_tokens = []
        
        if model_args.mm_use_im_patch_token:
            new_tokens.append(DEFAULT_IMAGE_PATCH_TOKEN)
        
        if model_args.mm_use_im_start_end:
            new_tokens.extend([DEFAULT_IM_START_TOKEN, DEFAULT_IM_END_TOKEN])
        
        if new_tokens:
            num_new_tokens = tokenizer.add_tokens(new_tokens, special_tokens=True)
            self.resize_token_embeddings(len(tokenizer))
            
            if num_new_tokens > 0:
                input_embeddings = self.get_input_embeddings().weight.data
                output_embeddings = self.get_output_embeddings().weight.data
                
                input_embeddings_avg = input_embeddings[:-num_new_tokens].mean(dim=0, keepdim=True) # keepdim for broadcasting
                output_embeddings_avg = output_embeddings[:-num_new_tokens].mean(dim=0, keepdim=True)
                
                input_embeddings[-num_new_tokens:] = input_embeddings_avg
                output_embeddings[-num_new_tokens:] = output_embeddings_avg
        
        if model_args.tune_mm_mlp_adapter:
            for p in self.get_input_embeddings().parameters():
                p.requires_grad = True
            for p in self.get_output_embeddings().parameters():
                p.requires_grad = False
        
        if model_args.pretrain_mm_mlp_adapter:
            self._load_pretrained_weights(model_args, num_new_tokens)

    def _load_pretrained_weights(self, model_args, num_new_tokens):
        mm_projector_weights = torch.load(model_args.pretrain_mm_mlp_adapter, map_location="cpu")
        embed_tokens_weight = mm_projector_weights["model.embed_tokens.weight"]
        input_embeddings = self.get_input_embeddings().weight.data
        
        if input_embeddings.shape == embed_tokens_weight.shape:
            input_embeddings[-num_new_tokens:] = embed_tokens_weight[-num_new_tokens:]
        elif embed_tokens_weight.shape[0] == num_new_tokens:
            input_embeddings[-num_new_tokens:] = embed_tokens_weight
        else:
            raise ValueError(f"Unexpected embed_tokens_weight shape. Pretrained: {embed_tokens_weight.shape}. Current: {input_embeddings.shape}. Number of new tokens: {num_new_tokens}.")