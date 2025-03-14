from typing import Any, Callable, Dict, List, Optional, Union
import numpy as np
import torch
from transformers import CLIPTextModel, CLIPTokenizer
from diffusers.image_processor import VaeImageProcessor
from diffusers.models import AutoencoderKL, UNet3DConditionModel
from diffusers.schedulers import KarrasDiffusionSchedulers
from diffusers.utils import (deprecate,
    logging,
    replace_example_docstring)
from diffusers.pipelines.text_to_video_synthesis import TextToVideoSDPipelineOutput
from torch.nn import functional as F
from diffusers.models.attention_processor import Attention
import math


TAU_2 = 15
TAU_1 = 10


def init_attention_params(unet, num_frames, lambda_=None, bs=None):
    
    
    for name, module in unet.named_modules():
        module_name = type(module).__name__
        if module_name == "Attention": 
            module.processor.LAMBDA = lambda_
            module.processor.bs = bs
            module.processor.num_frames = num_frames


def scaled_dot_product_attention(query, key, value, attn_mask=None, dropout_p=0.0,
        is_causal=False, scale=None, enable_gqa=False, k1 = None, d_l = None) -> torch.Tensor:
    
    L, S = query.size(-2), key.size(-2)
    scale_factor = 1 / math.sqrt(query.size(-1)) if scale is None else scale
    attn_bias = torch.zeros(L, S, dtype=query.dtype).to(query.device)
    if is_causal:
        assert attn_mask is None
        temp_mask = torch.ones(L, S, dtype=torch.bool).tril(diagonal=0)
        attn_bias.masked_fill_(temp_mask.logical_not(), float("-inf"))
        attn_bias.to(query.dtype)

    if attn_mask is not None:
        if attn_mask.dtype == torch.bool:
            attn_bias.masked_fill_(attn_mask.logical_not(), float("-inf"))
        else:
            attn_bias += attn_mask

    if enable_gqa:
        if k1 is not None and d_l is not None:
            k1 = k1.repeat_interleave(query.size(-3)//k1.size(-3), -3)
        key = key.repeat_interleave(query.size(-3)//key.size(-3), -3)
        value = value.repeat_interleave(query.size(-3)//value.size(-3), -3)

    if k1 is not None:
        attn_k1 = query @ k1.transpose(-2, -1)
        attn_weight = query @ key.transpose(-2, -1)
        attn_weight[:,:len(d_l),0] = attn_k1[:,:len(d_l),0] * d_l
        attn_weight = attn_weight * scale_factor
    else: 
        attn_weight = query @ key.transpose(-2, -1) * scale_factor

    attn_weight += attn_bias

    attn_weight = torch.softmax(attn_weight, dim=-1)
    attn_weight = torch.dropout(attn_weight, dropout_p, train=True)
    return attn_weight @ value

class AttnProcessor2_0:
    r"""
    Processor for implementing scaled dot-product attention (enabled by default if you're using PyTorch 2.0).
    """

    def __init__(self):
        if not hasattr(F, "scaled_dot_product_attention"):
            raise ImportError("AttnProcessor2_0 requires PyTorch 2.0, to use it, please upgrade PyTorch to 2.0.")
        
    def __call__(
        self,
        attn: Attention,
        hidden_states: torch.Tensor,
        encoder_hidden_states: Optional[torch.Tensor] = None,
        attention_mask: Optional[torch.Tensor] = None,
        temb: Optional[torch.Tensor] = None,
        *args,
        **kwargs,
    ) -> torch.Tensor:
        if len(args) > 0 or kwargs.get("scale", None) is not None:
            deprecation_message = "The `scale` argument is deprecated and will be ignored. Please remove it, as passing it will raise an error in the future. `scale` should directly be passed while calling the underlying pipeline component i.e., via `cross_attention_kwargs`."
            deprecate("scale", "1.0.0", deprecation_message)

        residual = hidden_states
        if attn.spatial_norm is not None:
            hidden_states = attn.spatial_norm(hidden_states, temb)

        input_ndim = hidden_states.ndim

        if input_ndim == 4:
            batch_size, channel, height, width = hidden_states.shape
            hidden_states = hidden_states.view(batch_size, channel, height * width).transpose(1, 2)

        batch_size, sequence_length, _ = (
            hidden_states.shape if encoder_hidden_states is None else encoder_hidden_states.shape
        )

        if attention_mask is not None:
            attention_mask = attn.prepare_attention_mask(attention_mask, sequence_length, batch_size)
            # scaled_dot_product_attention expects attention_mask shape to be
            # (batch, heads, source_length, target_length)
            attention_mask = attention_mask.view(batch_size, attn.heads, -1, attention_mask.shape[-1])

        if attn.group_norm is not None:
            hidden_states = attn.group_norm(hidden_states.transpose(1, 2)).transpose(1, 2)

        query = attn.to_q(hidden_states)

        if encoder_hidden_states is None:
            encoder_hidden_states = hidden_states
        elif attn.norm_cross:
            encoder_hidden_states = attn.norm_encoder_hidden_states(encoder_hidden_states)

        key = attn.to_k(encoder_hidden_states)
        value = attn.to_v(encoder_hidden_states)

        inner_dim = key.shape[-1]
        head_dim = inner_dim // attn.heads

        query, key, d_l, k1 = self.get_qk(query, key)

        
            

        query = query.view(batch_size, -1, attn.heads, head_dim).transpose(1, 2)

        key = key.view(batch_size, -1, attn.heads, head_dim).transpose(1, 2)
        value = value.view(batch_size, -1, attn.heads, head_dim).transpose(1, 2)
        
        # the output of sdp = (batch, num_heads, seq_len, head_dim)
        # TODO: add support for attn.scale when we move to Torch 2.1

        if d_l is not None:
            k1 = k1.view(batch_size, -1, attn.heads, head_dim).transpose(1, 2)
            hidden_states = scaled_dot_product_attention(
                query, key, value, attn_mask=attention_mask, dropout_p=0.0, is_causal=False, k1 = k1, d_l = d_l
            )
        else: 

            hidden_states = scaled_dot_product_attention(
                query, key, value, attn_mask=attention_mask, dropout_p=0.0, is_causal=False
            )

        hidden_states = hidden_states.transpose(1, 2).reshape(batch_size, -1, attn.heads * head_dim)
        hidden_states = hidden_states.to(query.dtype)

        # linear proj
        hidden_states = attn.to_out[0](hidden_states)
        # dropout
        hidden_states = attn.to_out[1](hidden_states)

        if input_ndim == 4:
            hidden_states = hidden_states.transpose(-1, -2).reshape(batch_size, channel, height, width)

        if attn.residual_connection:
            hidden_states = hidden_states + residual

        hidden_states = hidden_states / attn.rescale_output_factor

        return hidden_states
    
    def get_qk(
        self, query, key):
        r"""
        Compute the attention scores.

        Args:
            query (`torch.Tensor`): The query tensor.
            key (`torch.Tensor`): The key tensor.
            attention_mask (`torch.Tensor`, *optional*): The attention mask to use. If `None`, no mask is applied.

        Returns:
            `torch.Tensor`: The attention probabilities/scores.
        """
       
        
        
        q_old = query.clone()
        k_old = key.clone()
        dynamic_lambda = None
        key1 = None
        
        if self.use_last_attn_slice:# and self.last_attn_slice[0].shape[0] == query.shape[0]:# and query.shape[1]==self.num_frames:
            
            if self.last_attn_slice is not None:
                
                query_list = self.last_attn_slice[0]
                key_list = self.last_attn_slice[1]  


                if query.shape[1] == self.num_frames and query.shape == key.shape:
                    
                    key1 = key.clone()
                    key1[:,:1,:key_list.shape[2]] = key_list[:,:1]
                    dynamic_lambda = torch.tensor([1 + self.LAMBDA * (i/50) for i in range(self.num_frames)]).to(key.dtype).cuda()
                    
                if q_old.shape == k_old.shape and q_old.shape[1]!=self.num_frames:
                    
                    batch_dim = query_list.shape[0] // self.bs
                    all_dim = query.shape[0] // self.bs
                    for i in range(self.bs):
                        query[i*all_dim:(i*all_dim) + batch_dim,:query_list.shape[1],:query_list.shape[2]] = query_list[i*batch_dim:(i+1)*batch_dim]
                    
        if self.save_last_attn_slice:

            self.last_attn_slice = [
                query,
                key,
                ]

            self.save_last_attn_slice = False
    

        return query, key, dynamic_lambda, key1
    

def init_attention_func(unet):

    for name, module in unet.named_modules():
        module_name = type(module).__name__
        if module_name == "Attention":
            
            module.set_processor(AttnProcessor2_0())
            module.processor.last_attn_slice = None
            module.processor.use_last_attn_slice = False
            module.processor.save_last_attn_slice = False
            module.processor.LAMBDA = 0
            module.processor.num_frames = None
            module.processor.bs = 0
            
    
    return unet
            

def use_last_self_attention(unet, use=True):
    for name, module in unet.named_modules():
        module_name = type(module).__name__
        if module_name == "Attention" and "attn1" in name:
            module.processor.use_last_attn_slice = use
            
def save_last_self_attention(unet, save=True):
    for name, module in unet.named_modules():
        module_name = type(module).__name__
        if module_name == "Attention" and "attn1" in name:
            module.processor.save_last_attn_slice = save


logger = logging.get_logger(__name__)  # pylint: disable=invalid-name

EXAMPLE_DOC_STRING = """
    Examples:
        ```py
        >>> import torch
        >>> from diffusers import TextToVideoSDPipeline
        >>> from diffusers.utils import export_to_video

        >>> pipe = TextToVideoSDPipeline.from_pretrained(
        ...     "damo-vilab/text-to-video-ms-1.7b", torch_dtype=torch.float16, variant="fp16"
        ... )
        >>> pipe.enable_model_cpu_offload()

        >>> prompt = "Spiderman is surfing"
        >>> video_frames = pipe(prompt).frames[0]
        >>> video_path = export_to_video(video_frames)
        >>> video_path
        ```
"""


# Copied from diffusers.pipelines.animatediff.pipeline_animatediff.tensor2vid
def tensor2vid(video: torch.Tensor, processor: "VaeImageProcessor", output_type: str = "np"):
    batch_size, channels, num_frames, height, width = video.shape
    outputs = []
    for batch_idx in range(batch_size):
        batch_vid = video[batch_idx].permute(1, 0, 2, 3)
        batch_output = processor.postprocess(batch_vid, output_type)

        outputs.append(batch_output)

    if output_type == "np":
        outputs = np.stack(outputs)

    elif output_type == "pt":
        outputs = torch.stack(outputs)

    elif not output_type == "pil":
        raise ValueError(f"{output_type} does not exist. Please choose one of ['np', 'pt', 'pil']")

    return outputs

from diffusers import TextToVideoSDPipeline
class TextToVideoSDPipelineModded(TextToVideoSDPipeline):
    def __init__(
        self,
        vae: AutoencoderKL,
        text_encoder: CLIPTextModel,
        tokenizer: CLIPTokenizer,
        unet: UNet3DConditionModel,
        scheduler: KarrasDiffusionSchedulers,
    ):
        super().__init__(vae, text_encoder, tokenizer, unet, scheduler)
        

    def call_network(self,
                    negative_prompt_embeds,
                    prompt_embeds,
                    latents,
                    inv_latents,
                    t,
                    i,
                    null_embeds,
                    cross_attention_kwargs,
                    extra_step_kwargs,
                    do_classifier_free_guidance,
                    guidance_scale,
                    ):
         

        inv_latent_model_input = inv_latents
        inv_latent_model_input = self.scheduler.scale_model_input(inv_latent_model_input, t)
        
        latent_model_input = latents
        latent_model_input = self.scheduler.scale_model_input(latent_model_input, t)
        

        if do_classifier_free_guidance: 
            noise_pred_uncond = self.unet(
                latent_model_input,
                t,
                encoder_hidden_states=negative_prompt_embeds, 
                cross_attention_kwargs=cross_attention_kwargs,
                return_dict=False,
            )[0]

            noise_null_pred_uncond = self.unet(
                    inv_latent_model_input,
                    t,
                    encoder_hidden_states=negative_prompt_embeds, 
                    cross_attention_kwargs=cross_attention_kwargs,
                    return_dict=False,
                )[0]

                    
                    
        if i<=TAU_2:
            save_last_self_attention(self.unet)
            

            noise_null_pred = self.unet(
                    inv_latent_model_input,
                    t,
                    encoder_hidden_states=null_embeds, 
                    cross_attention_kwargs=cross_attention_kwargs,
                    return_dict=False,
                )[0]
            
            if do_classifier_free_guidance:
                noise_null_pred = noise_null_pred_uncond + guidance_scale * (noise_null_pred - noise_null_pred_uncond)
            
            bsz, channel, frames, width, height = inv_latents.shape
        
            inv_latents = inv_latents.permute(0, 2, 1, 3, 4).reshape(bsz*frames, channel, height, width)
            noise_null_pred = noise_null_pred.permute(0, 2, 1, 3, 4).reshape(bsz*frames, channel, height, width)
            inv_latents = self.scheduler.step(noise_null_pred, t, inv_latents, **extra_step_kwargs).prev_sample
            inv_latents = inv_latents[None, :].reshape((bsz, frames , -1) + inv_latents.shape[2:]).permute(0, 2, 1, 3, 4)

            use_last_self_attention(self.unet)
        else:
            noise_null_pred = None
            
        
    
        
        noise_pred = self.unet(
            latent_model_input,
            t,
            encoder_hidden_states=prompt_embeds, # For unconditional guidance
            cross_attention_kwargs=cross_attention_kwargs,
            return_dict=False,
        )[0]

        use_last_self_attention(self.unet, False)  
        

        if do_classifier_free_guidance:
            noise_pred_text = noise_pred
            noise_pred = noise_pred_uncond + guidance_scale * (noise_pred_text - noise_pred_uncond)

        # reshape latents
        bsz, channel, frames, width, height = latents.shape
        latents = latents.permute(0, 2, 1, 3, 4).reshape(bsz * frames, channel, width, height)
        noise_pred = noise_pred.permute(0, 2, 1, 3, 4).reshape(bsz * frames, channel, width, height)

        # compute the previous noisy sample x_t -> x_t-1
        latents = self.scheduler.step(noise_pred, t, latents, **extra_step_kwargs).prev_sample

        

        # reshape latents back
        latents = latents[None, :].reshape(bsz, frames, channel, width, height).permute(0, 2, 1, 3, 4)


        return {
            "latents": latents, 
            "inv_latents": inv_latents,
            "noise_pred": noise_pred,
            "noise_null_pred": noise_null_pred,
            }
                
    def optimize_latents(self, latents, inv_latents, t, i, null_embeds, cross_attention_kwargs, prompt_embeds):
        inv_scaled = self.scheduler.scale_model_input(inv_latents, t)  
                    
        noise_null_pred = self.unet(
            inv_scaled[:,:,0:1,:,:],
            t,
            encoder_hidden_states=null_embeds,
            cross_attention_kwargs=cross_attention_kwargs,
            return_dict=False,
        )[0]

        with torch.enable_grad():
            
            latent_train = latents[:,:,1:,:,:].clone().detach().requires_grad_(True)
            optimizer = torch.optim.Adam([latent_train], lr=1e-3)

            for j in range(10): 
                latent_in = torch.cat([inv_latents[:,:,0:1,:,:].detach(), latent_train], dim=2)
                latent_input_unet = self.scheduler.scale_model_input(latent_in, t)

                noise_pred = self.unet(
                    latent_input_unet,
                    t,
                    encoder_hidden_states=prompt_embeds, # For unconditional guidance
                    cross_attention_kwargs=cross_attention_kwargs,
                    return_dict=False,
                )[0]
                
                loss = torch.nn.functional.mse_loss(noise_pred[:,:,0,:,:], noise_null_pred[:,:,0,:,:])
                
                loss.backward()

                optimizer.step()
                optimizer.zero_grad()

                print("Iteration {} Subiteration {} Loss {} ".format(i, j, loss.item()))
            latents = latent_in.detach()
        return latents

    @torch.no_grad()
    @replace_example_docstring(EXAMPLE_DOC_STRING)
    def __call__(
        self,
        prompt: Union[str, List[str]] = None,
        height: Optional[int] = None,
        width: Optional[int] = None,
        num_frames: int = 16,
        num_inference_steps: int = 50,
        guidance_scale: float = 9.0,
        negative_prompt: Optional[Union[str, List[str]]] = None,
        eta: float = 0.0,
        generator: Optional[Union[torch.Generator, List[torch.Generator]]] = None,
        latents: Optional[torch.FloatTensor] = None,
        inv_latents: Optional[torch.FloatTensor] = None,
        prompt_embeds: Optional[torch.FloatTensor] = None,
        negative_prompt_embeds: Optional[torch.FloatTensor] = None,
        output_type: Optional[str] = "np",
        return_dict: bool = True,
        callback: Optional[Callable[[int, int, torch.FloatTensor], None]] = None,
        callback_steps: int = 1,
        cross_attention_kwargs: Optional[Dict[str, Any]] = None,
        clip_skip: Optional[int] = None,
        lambda_ = 0.5,
    ):
        r"""
        The call function to the pipeline for generation.

        Args:
            prompt (`str` or `List[str]`, *optional*):
                The prompt or prompts to guide image generation. If not defined, you need to pass `prompt_embeds`.
            height (`int`, *optional*, defaults to `self.unet.config.sample_size * self.vae_scale_factor`):
                The height in pixels of the generated video.
            width (`int`, *optional*, defaults to `self.unet.config.sample_size * self.vae_scale_factor`):
                The width in pixels of the generated video.
            num_frames (`int`, *optional*, defaults to 16):
                The number of video frames that are generated. Defaults to 16 frames which at 8 frames per seconds
                amounts to 2 seconds of video.
            num_inference_steps (`int`, *optional*, defaults to 50):
                The number of denoising steps. More denoising steps usually lead to a higher quality videos at the
                expense of slower inference.
            guidance_scale (`float`, *optional*, defaults to 7.5):
                A higher guidance scale value encourages the model to generate images closely linked to the text
                `prompt` at the expense of lower image quality. Guidance scale is enabled when `guidance_scale > 1`.
            negative_prompt (`str` or `List[str]`, *optional*):
                The prompt or prompts to guide what to not include in image generation. If not defined, you need to
                pass `negative_prompt_embeds` instead. Ignored when not using guidance (`guidance_scale < 1`).
            num_images_per_prompt (`int`, *optional*, defaults to 1):
                The number of images to generate per prompt.
            eta (`float`, *optional*, defaults to 0.0):
                Corresponds to parameter eta (η) from the [DDIM](https://arxiv.org/abs/2010.02502) paper. Only applies
                to the [`~schedulers.DDIMScheduler`], and is ignored in other schedulers.
            generator (`torch.Generator` or `List[torch.Generator]`, *optional*):
                A [`torch.Generator`](https://pytorch.org/docs/stable/generated/torch.Generator.html) to make
                generation deterministic.
            latents (`torch.FloatTensor`, *optional*):
                Pre-generated noisy latents sampled from a Gaussian distribution, to be used as inputs for video
                generation. Can be used to tweak the same generation with different prompts. If not provided, a latents
                tensor is generated by sampling using the supplied random `generator`. Latents should be of shape
                `(batch_size, num_channel, num_frames, height, width)`.
            prompt_embeds (`torch.FloatTensor`, *optional*):
                Pre-generated text embeddings. Can be used to easily tweak text inputs (prompt weighting). If not
                provided, text embeddings are generated from the `prompt` input argument.
            negative_prompt_embeds (`torch.FloatTensor`, *optional*):
                Pre-generated negative text embeddings. Can be used to easily tweak text inputs (prompt weighting). If
                not provided, `negative_prompt_embeds` are generated from the `negative_prompt` input argument.
            output_type (`str`, *optional*, defaults to `"np"`):
                The output format of the generated video. Choose between `torch.FloatTensor` or `np.array`.
            return_dict (`bool`, *optional*, defaults to `True`):
                Whether or not to return a [`~pipelines.text_to_video_synthesis.TextToVideoSDPipelineOutput`] instead
                of a plain tuple.
            callback (`Callable`, *optional*):
                A function that calls every `callback_steps` steps during inference. The function is called with the
                following arguments: `callback(step: int, timestep: int, latents: torch.FloatTensor)`.
            callback_steps (`int`, *optional*, defaults to 1):
                The frequency at which the `callback` function is called. If not specified, the callback is called at
                every step.
            cross_attention_kwargs (`dict`, *optional*):
                A kwargs dictionary that if specified is passed along to the [`AttentionProcessor`] as defined in
                [`self.processor`](https://github.com/huggingface/diffusers/blob/main/src/diffusers/models/attention_processor.py).
            clip_skip (`int`, *optional*):
                Number of layers to be skipped from CLIP while computing the prompt embeddings. A value of 1 means that
                the output of the pre-final layer will be used for computing the prompt embeddings.
        Examples:

        Returns:
            [`~pipelines.text_to_video_synthesis.TextToVideoSDPipelineOutput`] or `tuple`:
                If `return_dict` is `True`, [`~pipelines.text_to_video_synthesis.TextToVideoSDPipelineOutput`] is
                returned, otherwise a `tuple` is returned where the first element is a list with the generated frames.
        """
        # 0. Default height and width to unet
        height = height or self.unet.config.sample_size * self.vae_scale_factor
        width = width or self.unet.config.sample_size * self.vae_scale_factor

        num_images_per_prompt = 1

        # 1. Check inputs. Raise error if not correct
        self.check_inputs(
            prompt, height, width, callback_steps, negative_prompt, prompt_embeds, negative_prompt_embeds
        )

        # # 2. Define call parameters
        # if prompt is not None and isinstance(prompt, str):
        #     batch_size = 1
        # elif prompt is not None and isinstance(prompt, list):
        #     batch_size = len(prompt)
        # else:
        #     batch_size = prompt_embeds.shape[0]

        batch_size = inv_latents.shape[0]
        device = self._execution_device
        # here `guidance_scale` is defined analog to the guidance weight `w` of equation (2)
        # of the Imagen paper: https://arxiv.org/pdf/2205.11487.pdf . `guidance_scale = 1`
        # corresponds to doing no classifier free guidance.
        do_classifier_free_guidance = guidance_scale > 1.0

        # 3. Encode input prompt
        text_encoder_lora_scale = (
            cross_attention_kwargs.get("scale", None) if cross_attention_kwargs is not None else None
        )
        prompt_embeds, negative_prompt_embeds = self.encode_prompt(
            [prompt] * batch_size,
            device,
            num_images_per_prompt,
            do_classifier_free_guidance,
            [negative_prompt] * batch_size if negative_prompt is not None else None,
            prompt_embeds=prompt_embeds,
            negative_prompt_embeds=negative_prompt_embeds,
            lora_scale=text_encoder_lora_scale,
            clip_skip=clip_skip,
        )
        null_embeds, negative_prompt_embeds = self.encode_prompt(
            [""] * batch_size,
            device,
            num_images_per_prompt,
            do_classifier_free_guidance,
            [negative_prompt] * batch_size if negative_prompt is not None else None,
            prompt_embeds=None,
            negative_prompt_embeds=negative_prompt_embeds,
            lora_scale=text_encoder_lora_scale,
            clip_skip=clip_skip,
        )
        
        
        
        # 4. Prepare timesteps
        self.scheduler.set_timesteps(num_inference_steps, device=device)
        timesteps = self.scheduler.timesteps

        # 5. Prepare latent variables
        num_channels_latents = self.unet.config.in_channels
        latents = self.prepare_latents(
            batch_size * num_images_per_prompt,
            num_channels_latents,
            num_frames,
            height,
            width,
            prompt_embeds.dtype,
            device,
            generator,
            latents,
        )
        inv_latents = self.prepare_latents(
            batch_size * num_images_per_prompt,
            num_channels_latents,
            num_frames,
            height,
            width,
            prompt_embeds.dtype,
            device,
            generator,
            inv_latents,
        )

        # 6. Prepare extra step kwargs. TODO: Logic should ideally just be moved out of the pipeline
        extra_step_kwargs = self.prepare_extra_step_kwargs(generator, eta)
        
        # 7. Denoising loop
        num_warmup_steps = len(timesteps) - num_inference_steps * self.scheduler.order
        
        init_attention_func(self.unet)
        print("Setup for Current Run")
        print("----------------------")
        print("Prompt ", prompt)
        print("Batch size ", batch_size)
        print("Num frames ", latents.shape[2])
        print("Lambda ", lambda_)
        
        init_attention_params(self.unet, num_frames=latents.shape[2], lambda_=lambda_, bs = batch_size)
        
        iters_to_alter = [-1]#i for i in range(0, TAU_1)]
    

        with self.progress_bar(total=num_inference_steps) as progress_bar:
            
            mask_in = torch.zeros(latents.shape).to(dtype=latents.dtype, device=latents.device)
            mask_in[:, :, 0, :, :] = 1
            assert latents.shape[0] == inv_latents.shape[0], "Latents and Inverse Latents should have the same batch but got {} and {}".format(latents.shape[0], inv_latents.shape[0])
            inv_latents = inv_latents.repeat(1,1,num_frames,1,1)

            latents = inv_latents * mask_in + latents * (1-mask_in)
            
            

            for i, t in enumerate(timesteps):
                
                curr_copy = max(1,num_frames - i)
                inv_latents = inv_latents[:,:,:curr_copy, :, : ]
                if i in iters_to_alter:

                    latents = self.optimize_latents(latents, inv_latents, t, i, null_embeds, cross_attention_kwargs, prompt_embeds)
                

                output_dict = self.call_network(
                        negative_prompt_embeds,
                        prompt_embeds,
                        latents,
                        inv_latents,
                        t,
                        i,
                        null_embeds,
                        cross_attention_kwargs,
                        extra_step_kwargs,
                        do_classifier_free_guidance,
                        guidance_scale,
                    )
                latents = output_dict["latents"]
                inv_latents = output_dict["inv_latents"]
               
                # call the callback, if provided
                if i == len(timesteps) - 1 or ((i + 1) > num_warmup_steps and (i + 1) % self.scheduler.order == 0):
                    progress_bar.update()
                    if callback is not None and i % callback_steps == 0:
                        step_idx = i // getattr(self.scheduler, "order", 1)
                        callback(step_idx, t, latents)

        # 8. Post processing
        if output_type == "latent":
            video = latents
        else:
            video_tensor = self.decode_latents(latents)
            video = tensor2vid(video_tensor, self.image_processor, output_type)

        # 9. Offload all models
        self.maybe_free_model_hooks()

        if not return_dict:
            return (video,)

        return TextToVideoSDPipelineOutput(frames=video)