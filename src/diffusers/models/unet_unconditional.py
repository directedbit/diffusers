import functools
import math

import numpy as np
from typing import Dict, Union

import torch
import torch.nn as nn

from ..configuration_utils import ConfigMixin
from ..modeling_utils import ModelMixin
from .attention import AttentionBlock
from .embeddings import GaussianFourierProjection, get_timestep_embedding
from .resnet import Downsample2D, FirDownsample2D, FirUpsample2D, ResnetBlock2D, Upsample2D
from .unet_new import UNetMidBlock2D, get_down_block, get_up_block


class Combine(nn.Module):
    """Combine information from skip connections."""

    def __init__(self, dim1, dim2, method="cat"):
        super().__init__()
        # 1x1 convolution with DDPM initialization.
        self.Conv_0 = nn.Conv2d(dim1, dim2, kernel_size=1, padding=0)
        self.method = method

    def forward(self, x, y):
        h = self.Conv_0(x)
        if self.method == "cat":
            return torch.cat([h, y], dim=1)
        elif self.method == "sum":
            return h + y
        else:
            raise ValueError(f"Method {self.method} not recognized.")


class TimestepEmbedding(nn.Module):
    def __init__(self, channel, time_embed_dim, act_fn="silu"):
        super().__init__()

        self.linear_1 = nn.Linear(channel, time_embed_dim)
        self.act = None
        if act_fn == "silu":
            self.act = nn.SiLU()
        self.linear_2 = nn.Linear(time_embed_dim, time_embed_dim)

    def forward(self, sample):
        sample = self.linear_1(sample)

        if self.act is not None:
            sample = self.act(sample)

        sample = self.linear_2(sample)
        return sample


class Timesteps(nn.Module):
    def __init__(self, num_channels, flip_sin_to_cos, downscale_freq_shift):
        super().__init__()
        self.num_channels = num_channels
        self.flip_sin_to_cos = flip_sin_to_cos
        self.downscale_freq_shift = downscale_freq_shift

    def forward(self, timesteps):
        t_emb = get_timestep_embedding(
            timesteps,
            self.num_channels,
            flip_sin_to_cos=self.flip_sin_to_cos,
            downscale_freq_shift=self.downscale_freq_shift,
        )
        return t_emb


class UNetUnconditionalModel(ModelMixin, ConfigMixin):
    """
    The full UNet model with attention and timestep embedding. :param in_channels: channels in the input Tensor. :param
    model_channels: base channel count for the model. :param out_channels: channels in the output Tensor. :param
    num_res_blocks: number of residual blocks per downsample. :param attention_resolutions: a collection of downsample
    rates at which
        attention will take place. May be a set, list, or tuple. For example, if this contains 4, then at 4x
        downsampling, attention will be used.
    :param dropout: the dropout probability. :param channel_mult: channel multiplier for each level of the UNet. :param
    conv_resample: if True, use learned convolutions for upsampling and
        downsampling.
    :param dims: determines if the signal is 1D, 2D, or 3D. :param num_classes: if specified (as an int), then this
    model will be
        class-conditional with `num_classes` classes.
    :param use_checkpoint: use gradient checkpointing to reduce memory usage. :param num_heads: the number of attention
    heads in each attention layer. :param num_heads_channels: if specified, ignore num_heads and instead use
                               a fixed channel width per attention head.
    :param num_heads_upsample: works with num_heads to set a different number
                               of heads for upsampling. Deprecated.
    :param use_scale_shift_norm: use a FiLM-like conditioning mechanism. :param resblock_updown: use residual blocks
    for up/downsampling. :param use_new_attention_order: use a different attention pattern for potentially
                                    increased efficiency.
    """

    def __init__(
        self,
        image_size=None,
        in_channels=None,
        out_channels=None,
        num_res_blocks=None,
        dropout=0,
        block_channels=(224, 448, 672, 896),
        down_blocks=(
            "UNetResDownBlock2D",
            "UNetResAttnDownBlock2D",
            "UNetResAttnDownBlock2D",
            "UNetResAttnDownBlock2D",
        ),
        downsample_padding=1,
        up_blocks=("UNetResAttnUpBlock2D", "UNetResAttnUpBlock2D", "UNetResAttnUpBlock2D", "UNetResUpBlock2D"),
        resnet_act_fn="silu",
        resnet_eps=1e-5,
        conv_resample=True,
        num_head_channels=32,
        flip_sin_to_cos=True,
        downscale_freq_shift=0,
        time_embedding_type="positional",
        time_embedding_act_fn="silu",
        # TODO(PVP) - to delete later at release
        # IMPORTANT: NOT RELEVANT WHEN REVIEWING API
        # ======================================
        # LDM
        attention_resolutions=(8, 4, 2),
        ldm=False,
        # DDPM
        out_ch=None,
        resolution=None,
        attn_resolutions=None,
        resamp_with_conv=None,
        ch_mult=None,
        ch=None,
        ddpm=False,
        # SDE
        sde=True,
        nf=None,
        fir=None,
        progressive=None,
        progressive_combine=None,
        scale_by_sigma=None,
        skip_rescale=None,
        num_channels=None,
        centered=False,
        conditional=True,
        conv_size=3,
        fir_kernel=(1, 3, 3, 1),
        fourier_scale=16,
        init_scale=0.0,
        progressive_input="input_skip",
        continuous=True,
    ):
        super().__init__()
        # register all __init__ params to be accessible via `self.config.<...>`
        # should probably be automated down the road as this is pure boiler plate code
        self.register_to_config(
            image_size=image_size,
            in_channels=in_channels,
            block_channels=block_channels,
            downsample_padding=downsample_padding,
            out_channels=out_channels,
            num_res_blocks=num_res_blocks,
            down_blocks=down_blocks,
            up_blocks=up_blocks,
            dropout=dropout,
            conv_resample=conv_resample,
            num_head_channels=num_head_channels,
            flip_sin_to_cos=flip_sin_to_cos,
            downscale_freq_shift=downscale_freq_shift,
            time_embedding_type=time_embedding_type,
            time_embedding_act_fn=time_embedding_act_fn,
            attention_resolutions=attention_resolutions,
            attn_resolutions=attn_resolutions,
            ldm=ldm,
            ddpm=ddpm,
            sde=sde,
        )
        if sde:
            sampling_filter = "fir" if fir else "default"
            block_channels = [nf * x for x in ch_mult]
            in_channels = out_channels = num_channels
            conv_resample = resamp_with_conv
            time_embedding_type = "fourier"
            self.config.time_embedding_type = time_embedding_type
            self.config.time_embedding_act_fn = None
            down_blocks=(
                "UNetResSkipDownBlock2D",
                "UNetResAttnSkipDownBlock2D",
                "UNetResSkipDownBlock2D",
                "UNetResSkipDownBlock2D",
            )

        # TODO(PVP) - to delete later at release
        # IMPORTANT: NOT RELEVANT WHEN REVIEWING API
        # ======================================
        self.image_size = image_size
        time_embed_dim = block_channels[0] * 4
        # ======================================

        # input
        self.conv_in = nn.Conv2d(in_channels, block_channels[0], kernel_size=3, padding=(1, 1))

        # time
        if self.config.time_embedding_type == "fourier":
            self.time_steps = GaussianFourierProjection(embedding_size=nf, scale=fourier_scale)
            timestep_input_dim = 2 * block_channels[0]
        elif self.config.time_embedding_type == "positional":
            self.time_steps = Timesteps(self.config.block_channels[0], flip_sin_to_cos, downscale_freq_shift)
            timestep_input_dim = block_channels[0]

        self.time_embedding = TimestepEmbedding(timestep_input_dim, time_embed_dim, act_fn=self.config.time_embedding_act_fn)

        self.downsample_blocks = nn.ModuleList([])
        self.mid = None
        self.upsample_blocks = nn.ModuleList([])

        # down
        output_channel = block_channels[0]
        for i, down_block_type in enumerate(down_blocks):
            input_channel = output_channel
            output_channel = block_channels[i]
            is_final_block = i == len(block_channels) - 1

            down_block = get_down_block(
                down_block_type,
                num_layers=num_res_blocks,
                in_channels=input_channel,
                out_channels=output_channel,
                temb_channels=time_embed_dim,
                add_downsample=not is_final_block,
                resnet_eps=resnet_eps,
                resnet_act_fn=resnet_act_fn,
                attn_num_head_channels=num_head_channels,
                downsample_padding=downsample_padding,
            )
            self.downsample_blocks.append(down_block)

        # mid
        if self.config.ddpm:
            self.mid_new_2 = UNetMidBlock2D(
                in_channels=block_channels[-1],
                dropout=dropout,
                temb_channels=time_embed_dim,
                resnet_eps=resnet_eps,
                resnet_act_fn=resnet_act_fn,
                resnet_time_scale_shift="default",
                attn_num_head_channels=num_head_channels,
            )
        else:
            self.mid = UNetMidBlock2D(
                in_channels=block_channels[-1],
                dropout=dropout,
                temb_channels=time_embed_dim,
                resnet_eps=resnet_eps,
                resnet_act_fn=resnet_act_fn,
                resnet_time_scale_shift="default",
                attn_num_head_channels=num_head_channels,
            )

        # up
        reversed_block_channels = list(reversed(block_channels))
        output_channel = reversed_block_channels[0]
        for i, up_block_type in enumerate(up_blocks):
            prev_output_channel = output_channel
            output_channel = reversed_block_channels[i]
            input_channel = reversed_block_channels[min(i + 1, len(block_channels) - 1)]

            is_final_block = i == len(block_channels) - 1

            up_block = get_up_block(
                up_block_type,
                num_layers=num_res_blocks + 1,
                in_channels=input_channel,
                out_channels=output_channel,
                prev_output_channel=prev_output_channel,
                temb_channels=time_embed_dim,
                add_upsample=not is_final_block,
                resnet_eps=resnet_eps,
                resnet_act_fn=resnet_act_fn,
                attn_num_head_channels=num_head_channels,
            )
            self.upsample_blocks.append(up_block)
            prev_output_channel = output_channel

        # out
        self.conv_norm_out = nn.GroupNorm(num_channels=block_channels[0], num_groups=32, eps=resnet_eps)
        self.conv_act = nn.SiLU()
        self.conv_out = nn.Conv2d(block_channels[0], out_channels, 3, padding=1)

        # ======================== Out ====================

        # =========== TO DELETE AFTER CONVERSION ==========
        # TODO(PVP) - to delete later at release
        # IMPORTANT: NOT RELEVANT WHEN REVIEWING API
        # ======================================
        self.is_overwritten = False
        if ldm:
            transformer_depth = 1
            context_dim = None
            legacy = True
            num_heads = -1
            model_channels = block_channels[0]
            channel_mult = tuple([x // model_channels for x in block_channels])
            self.init_for_ldm(
                in_channels,
                model_channels,
                channel_mult,
                num_res_blocks,
                dropout,
                time_embed_dim,
                attention_resolutions,
                num_head_channels,
                num_heads,
                legacy,
                False,
                transformer_depth,
                context_dim,
                conv_resample,
                out_channels,
            )
        elif ddpm:
            out_channels = out_ch
            image_size = resolution
            block_channels = [x * ch for x in ch_mult]
            conv_resample = resamp_with_conv
            out_ch = out_channels
            resolution = image_size
            ch = block_channels[0]
            ch_mult = [b // ch for b in block_channels]
            resamp_with_conv = conv_resample
            self.init_for_ddpm(
                ch_mult,
                ch,
                num_res_blocks,
                resolution,
                in_channels,
                resamp_with_conv,
                attn_resolutions,
                out_ch,
                dropout=0.1,
            )
        elif sde:
            self.init_for_sde(
                image_size,
                num_channels,
                centered,
                attn_resolutions,
                ch_mult,
                conditional,
                conv_size,
                dropout,
                time_embedding_type,
                fir,
                fir_kernel,
                fourier_scale,
                init_scale,
                nf,
                num_res_blocks,
                progressive,
                progressive_combine,
                progressive_input,
                resamp_with_conv,
                scale_by_sigma,
                skip_rescale,
                continuous,
            )

    def forward(
        self, sample: torch.FloatTensor, timestep: Union[torch.Tensor, float, int]
    ) -> Dict[str, torch.FloatTensor]:
        # TODO(PVP) - to delete later at release
        # IMPORTANT: NOT RELEVANT WHEN REVIEWING API
        # ======================================
        if not self.is_overwritten:
            self.set_weights()

        # 1. time step embeddings -> make correct tensor
        timesteps = timestep
        if not torch.is_tensor(timesteps):
            timesteps = torch.tensor([timesteps], dtype=torch.long, device=sample.device)
        elif torch.is_tensor(timesteps) and len(timesteps.shape) == 0:
            timesteps = timesteps[None].to(sample.device)

        t_emb = self.time_steps(timesteps)
        emb = self.time_embedding(t_emb)

        # 2. pre-process sample
        skip_sample = sample
        sample = self.conv_in(sample)

        print("Sample before up", sample.abs().sum())

        # 3. down blocks
        down_block_res_samples = (sample,)
        for downsample_block in self.downsample_blocks:
            if hasattr(downsample_block, "skip_conv") and downsample_block.skip_connection is not None:
                sample, res_samples, skip_sample = downsample_block(hidden_states=sample, temb=emb, skip_sample=skip_sample)
            else:
                sample, res_samples = downsample_block(hidden_states=sample, temb=emb)

            # append to tuple
            down_block_res_samples += res_samples

        # 4. mid block
        if self.config.ddpm:
            sample = self.mid_new_2(sample, emb)
        else:
            sample = self.mid(sample, emb)

        # 5. up blocks
        for upsample_block in self.upsample_blocks:

            # pop from tuple
            res_samples = down_block_res_samples[-len(upsample_block.resnets) :]
            down_block_res_samples = down_block_res_samples[: -len(upsample_block.resnets)]

            sample = upsample_block(sample, res_samples, emb)

        # 6. post-process sample
        sample = self.conv_norm_out(sample)
        sample = self.conv_act(sample)
        sample = self.conv_out(sample)

        output = {"sample": sample}

        return output

    # !!!IMPORTANT - ALL OF THE FOLLOWING CODE WILL BE DELETED AT RELEASE TIME AND SHOULD NOT BE TAKEN INTO CONSIDERATION WHEN EVALUATING THE API ###
    # =================================================================================================================================================

    def set_weights(self):
        self.is_overwritten = True
        if self.config.ldm:
            self.time_embedding.linear_1.weight.data = self.time_embed[0].weight.data
            self.time_embedding.linear_1.bias.data = self.time_embed[0].bias.data
            self.time_embedding.linear_2.weight.data = self.time_embed[2].weight.data
            self.time_embedding.linear_2.bias.data = self.time_embed[2].bias.data

            # ================ SET WEIGHTS OF ALL WEIGHTS ==================
            for i, input_layer in enumerate(self.input_blocks[1:]):
                block_id = i // (self.config.num_res_blocks + 1)
                layer_in_block_id = i % (self.config.num_res_blocks + 1)

                if layer_in_block_id == 2:
                    self.downsample_blocks[block_id].downsamplers[0].conv.weight.data = input_layer[0].op.weight.data
                    self.downsample_blocks[block_id].downsamplers[0].conv.bias.data = input_layer[0].op.bias.data
                elif len(input_layer) > 1:
                    self.downsample_blocks[block_id].resnets[layer_in_block_id].set_weight(input_layer[0])
                    self.downsample_blocks[block_id].attentions[layer_in_block_id].set_weight(input_layer[1])
                else:
                    self.downsample_blocks[block_id].resnets[layer_in_block_id].set_weight(input_layer[0])

            self.mid.resnets[0].set_weight(self.middle_block[0])
            self.mid.resnets[1].set_weight(self.middle_block[2])
            self.mid.attentions[0].set_weight(self.middle_block[1])

            for i, input_layer in enumerate(self.output_blocks):
                block_id = i // (self.config.num_res_blocks + 1)
                layer_in_block_id = i % (self.config.num_res_blocks + 1)

                if len(input_layer) > 2:
                    self.upsample_blocks[block_id].resnets[layer_in_block_id].set_weight(input_layer[0])
                    self.upsample_blocks[block_id].attentions[layer_in_block_id].set_weight(input_layer[1])
                    self.upsample_blocks[block_id].upsamplers[0].conv.weight.data = input_layer[2].conv.weight.data
                    self.upsample_blocks[block_id].upsamplers[0].conv.bias.data = input_layer[2].conv.bias.data
                elif len(input_layer) > 1 and "Upsample2D" in input_layer[1].__class__.__name__:
                    self.upsample_blocks[block_id].resnets[layer_in_block_id].set_weight(input_layer[0])
                    self.upsample_blocks[block_id].upsamplers[0].conv.weight.data = input_layer[1].conv.weight.data
                    self.upsample_blocks[block_id].upsamplers[0].conv.bias.data = input_layer[1].conv.bias.data
                elif len(input_layer) > 1:
                    self.upsample_blocks[block_id].resnets[layer_in_block_id].set_weight(input_layer[0])
                    self.upsample_blocks[block_id].attentions[layer_in_block_id].set_weight(input_layer[1])
                else:
                    self.upsample_blocks[block_id].resnets[layer_in_block_id].set_weight(input_layer[0])

            self.conv_in.weight.data = self.input_blocks[0][0].weight.data
            self.conv_in.bias.data = self.input_blocks[0][0].bias.data

            self.conv_norm_out.weight.data = self.out[0].weight.data
            self.conv_norm_out.bias.data = self.out[0].bias.data
            self.conv_out.weight.data = self.out[2].weight.data
            self.conv_out.bias.data = self.out[2].bias.data

            self.remove_ldm()

        elif self.config.ddpm:
            self.time_embedding.linear_1.weight.data = self.temb.dense[0].weight.data
            self.time_embedding.linear_1.bias.data = self.temb.dense[0].bias.data
            self.time_embedding.linear_2.weight.data = self.temb.dense[1].weight.data
            self.time_embedding.linear_2.bias.data = self.temb.dense[1].bias.data

            for i, block in enumerate(self.down):
                if hasattr(block, "downsample"):
                    self.downsample_blocks[i].downsamplers[0].conv.weight.data = block.downsample.conv.weight.data
                    self.downsample_blocks[i].downsamplers[0].conv.bias.data = block.downsample.conv.bias.data
                if hasattr(block, "block") and len(block.block) > 0:
                    for j in range(self.num_res_blocks):
                        self.downsample_blocks[i].resnets[j].set_weight(block.block[j])
                if hasattr(block, "attn") and len(block.attn) > 0:
                    for j in range(self.num_res_blocks):
                        self.downsample_blocks[i].attentions[j].set_weight(block.attn[j])

            self.mid_new_2.resnets[0].set_weight(self.mid.block_1)
            self.mid_new_2.resnets[1].set_weight(self.mid.block_2)
            self.mid_new_2.attentions[0].set_weight(self.mid.attn_1)

            for i, block in enumerate(self.up):
                k = len(self.up) - 1 - i
                if hasattr(block, "upsample"):
                    self.upsample_blocks[k].upsamplers[0].conv.weight.data = block.upsample.conv.weight.data
                    self.upsample_blocks[k].upsamplers[0].conv.bias.data = block.upsample.conv.bias.data
                if hasattr(block, "block") and len(block.block) > 0:
                    for j in range(self.num_res_blocks + 1):
                        self.upsample_blocks[k].resnets[j].set_weight(block.block[j])
                if hasattr(block, "attn") and len(block.attn) > 0:
                    for j in range(self.num_res_blocks + 1):
                        self.upsample_blocks[k].attentions[j].set_weight(block.attn[j])

            self.conv_norm_out.weight.data = self.norm_out.weight.data
            self.conv_norm_out.bias.data = self.norm_out.bias.data

            self.remove_ddpm()
        elif self.config.sde:
            self.time_steps.weight = self.all_modules[0].weight
            self.time_embedding.linear_1.weight.data = self.all_modules[1].weight.data
            self.time_embedding.linear_1.bias.data = self.all_modules[1].bias.data
            self.time_embedding.linear_2.weight.data = self.all_modules[2].weight.data
            self.time_embedding.linear_2.bias.data = self.all_modules[2].bias.data

            self.conv_in.weight.data = self.all_modules[3].weight.data
            self.conv_in.bias.data = self.all_modules[3].bias.data

            import ipdb; ipdb.set_trace()

    def init_for_ddpm(
        self,
        ch_mult,
        ch,
        num_res_blocks,
        resolution,
        in_channels,
        resamp_with_conv,
        attn_resolutions,
        out_ch,
        dropout=0.1,
    ):
        ch_mult = tuple(ch_mult)
        self.ch = ch
        self.temb_ch = self.ch * 4
        self.num_resolutions = len(ch_mult)
        self.num_res_blocks = num_res_blocks
        self.resolution = resolution

        # timestep embedding
        self.temb = nn.Module()
        self.temb.dense = nn.ModuleList(
            [
                torch.nn.Linear(self.ch, self.temb_ch),
                torch.nn.Linear(self.temb_ch, self.temb_ch),
            ]
        )

        # downsampling
        self.conv_in = torch.nn.Conv2d(in_channels, self.ch, kernel_size=3, stride=1, padding=1)

        curr_res = resolution
        in_ch_mult = (1,) + ch_mult
        self.down = nn.ModuleList()
        for i_level in range(self.num_resolutions):
            block = nn.ModuleList()
            attn = nn.ModuleList()
            block_in = ch * in_ch_mult[i_level]
            block_out = ch * ch_mult[i_level]
            for i_block in range(self.num_res_blocks):
                block.append(
                    ResnetBlock2D(
                        in_channels=block_in, out_channels=block_out, temb_channels=self.temb_ch, dropout=dropout
                    )
                )
                block_in = block_out
                if curr_res in attn_resolutions:
                    attn.append(AttentionBlock(block_in, overwrite_qkv=True))
            down = nn.Module()
            down.block = block
            down.attn = attn
            if i_level != self.num_resolutions - 1:
                down.downsample = Downsample2D(block_in, use_conv=resamp_with_conv, padding=0)
                curr_res = curr_res // 2
            self.down.append(down)

        # middle
        self.mid = nn.Module()
        self.mid.block_1 = ResnetBlock2D(
            in_channels=block_in, out_channels=block_in, temb_channels=self.temb_ch, dropout=dropout
        )
        self.mid.attn_1 = AttentionBlock(block_in, overwrite_qkv=True)
        self.mid.block_2 = ResnetBlock2D(
            in_channels=block_in, out_channels=block_in, temb_channels=self.temb_ch, dropout=dropout
        )
        self.mid_new = UNetMidBlock2D(in_channels=block_in, temb_channels=self.temb_ch, dropout=dropout)
        self.mid_new.resnets[0] = self.mid.block_1
        self.mid_new.attentions[0] = self.mid.attn_1
        self.mid_new.resnets[1] = self.mid.block_2

        # upsampling
        self.up = nn.ModuleList()
        for i_level in reversed(range(self.num_resolutions)):
            block = nn.ModuleList()
            attn = nn.ModuleList()
            block_out = ch * ch_mult[i_level]
            skip_in = ch * ch_mult[i_level]
            for i_block in range(self.num_res_blocks + 1):
                if i_block == self.num_res_blocks:
                    skip_in = ch * in_ch_mult[i_level]
                block.append(
                    ResnetBlock2D(
                        in_channels=block_in + skip_in,
                        out_channels=block_out,
                        temb_channels=self.temb_ch,
                        dropout=dropout,
                    )
                )
                block_in = block_out
                if curr_res in attn_resolutions:
                    attn.append(AttentionBlock(block_in, overwrite_qkv=True))
            up = nn.Module()
            up.block = block
            up.attn = attn
            if i_level != 0:
                up.upsample = Upsample2D(block_in, use_conv=resamp_with_conv)
                curr_res = curr_res * 2
            self.up.insert(0, up)  # prepend to get consistent order

        # end
        self.norm_out = Normalize(block_in)
        self.conv_out = torch.nn.Conv2d(block_in, out_ch, kernel_size=3, stride=1, padding=1)

    def init_for_ldm(
        self,
        in_channels,
        model_channels,
        channel_mult,
        num_res_blocks,
        dropout,
        time_embed_dim,
        attention_resolutions,
        num_head_channels,
        num_heads,
        legacy,
        use_spatial_transformer,
        transformer_depth,
        context_dim,
        conv_resample,
        out_channels,
    ):
        # TODO(PVP) - delete after weight conversion
        class TimestepEmbedSequential(nn.Sequential):
            """
            A sequential module that passes timestep embeddings to the children that support it as an extra input.
            """

            pass

        # TODO(PVP) - delete after weight conversion
        def conv_nd(dims, *args, **kwargs):
            """
            Create a 1D, 2D, or 3D convolution module.
            """
            if dims == 1:
                return nn.Conv1d(*args, **kwargs)
            elif dims == 2:
                return nn.Conv2d(*args, **kwargs)
            elif dims == 3:
                return nn.Conv3d(*args, **kwargs)
            raise ValueError(f"unsupported dimensions: {dims}")

        self.time_embed = nn.Sequential(
            nn.Linear(model_channels, time_embed_dim),
            nn.SiLU(),
            nn.Linear(time_embed_dim, time_embed_dim),
        )

        dims = 2
        self.input_blocks = nn.ModuleList(
            [TimestepEmbedSequential(conv_nd(dims, in_channels, model_channels, 3, padding=1))]
        )

        self._feature_size = model_channels
        input_block_chans = [model_channels]
        ch = model_channels
        ds = 1
        for level, mult in enumerate(channel_mult):
            for _ in range(num_res_blocks):
                layers = [
                    ResnetBlock2D(
                        in_channels=ch,
                        out_channels=mult * model_channels,
                        dropout=dropout,
                        temb_channels=time_embed_dim,
                        eps=1e-5,
                        non_linearity="silu",
                        overwrite_for_ldm=True,
                    )
                ]
                ch = mult * model_channels
                if ds in attention_resolutions:
                    if num_head_channels == -1:
                        dim_head = ch // num_heads
                    else:
                        num_heads = ch // num_head_channels
                        dim_head = num_head_channels
                    if legacy:
                        # num_heads = 1
                        dim_head = num_head_channels
                    layers.append(
                        AttentionBlock(
                            ch,
                            num_heads=num_heads,
                            num_head_channels=dim_head,
                        ),
                    )
                self.input_blocks.append(TimestepEmbedSequential(*layers))
                self._feature_size += ch
                input_block_chans.append(ch)
            if level != len(channel_mult) - 1:
                out_ch = ch
                self.input_blocks.append(
                    TimestepEmbedSequential(
                        Downsample2D(ch, use_conv=conv_resample, out_channels=out_ch, padding=1, name="op")
                    )
                )
                ch = out_ch
                input_block_chans.append(ch)
                ds *= 2
                self._feature_size += ch

        if num_head_channels == -1:
            dim_head = ch // num_heads
        else:
            num_heads = ch // num_head_channels
            dim_head = num_head_channels
        if legacy:
            # num_heads = 1
            dim_head = num_head_channels

        if dim_head < 0:
            dim_head = None

        # TODO(Patrick) - delete after weight conversion
        # init to be able to overwrite `self.mid`
        self.middle_block = TimestepEmbedSequential(
            ResnetBlock2D(
                in_channels=ch,
                out_channels=None,
                dropout=dropout,
                temb_channels=time_embed_dim,
                eps=1e-5,
                non_linearity="silu",
                overwrite_for_ldm=True,
            ),
            AttentionBlock(
                ch,
                num_heads=num_heads,
                num_head_channels=dim_head,
            ),
            ResnetBlock2D(
                in_channels=ch,
                out_channels=None,
                dropout=dropout,
                temb_channels=time_embed_dim,
                eps=1e-5,
                non_linearity="silu",
                overwrite_for_ldm=True,
            ),
        )
        self._feature_size += ch

        self.output_blocks = nn.ModuleList([])
        for level, mult in list(enumerate(channel_mult))[::-1]:
            for i in range(num_res_blocks + 1):
                ich = input_block_chans.pop()
                layers = [
                    ResnetBlock2D(
                        in_channels=ch + ich,
                        out_channels=model_channels * mult,
                        dropout=dropout,
                        temb_channels=time_embed_dim,
                        eps=1e-5,
                        non_linearity="silu",
                        overwrite_for_ldm=True,
                    ),
                ]
                ch = model_channels * mult
                if ds in attention_resolutions:
                    if num_head_channels == -1:
                        dim_head = ch // num_heads
                    else:
                        num_heads = ch // num_head_channels
                        dim_head = num_head_channels
                    if legacy:
                        # num_heads = 1
                        dim_head = num_head_channels
                    layers.append(
                        AttentionBlock(
                            ch,
                            num_heads=-1,
                            num_head_channels=dim_head,
                        ),
                    )
                if level and i == num_res_blocks:
                    out_ch = ch
                    layers.append(Upsample2D(ch, use_conv=conv_resample, out_channels=out_ch))
                    ds //= 2
                self.output_blocks.append(TimestepEmbedSequential(*layers))
                self._feature_size += ch

        self.out = nn.Sequential(
            nn.GroupNorm(num_channels=model_channels, num_groups=32, eps=1e-5),
            nn.SiLU(),
            nn.Conv2d(model_channels, out_channels, 3, padding=1),
        )

    def init_for_sde(
        self,
        image_size,
        num_channels,
        centered,
        attn_resolutions,
        ch_mult,
        conditional,
        conv_size,
        dropout,
        embedding_type,
        fir,
        fir_kernel,
        fourier_scale,
        init_scale,
        nf,
        num_res_blocks,
        progressive,
        progressive_combine,
        progressive_input,
        resamp_with_conv,
        scale_by_sigma,
        skip_rescale,
        continuous,
    ):
        self.act = nn.SiLU()
        self.nf = nf
        self.num_res_blocks = num_res_blocks
        self.attn_resolutions = attn_resolutions
        self.num_resolutions = len(ch_mult)
        self.all_resolutions = all_resolutions = [image_size // (2**i) for i in range(self.num_resolutions)]

        self.conditional = conditional
        self.skip_rescale = skip_rescale
        self.progressive = progressive
        self.progressive_input = progressive_input
        self.embedding_type = embedding_type
        assert progressive in ["none", "output_skip", "residual"]
        assert progressive_input in ["none", "input_skip", "residual"]
        assert embedding_type in ["fourier", "positional"]
        combine_method = progressive_combine.lower()
        combiner = functools.partial(Combine, method=combine_method)

        modules = []
        # timestep/noise_level embedding; only for continuous training
        if embedding_type == "fourier":
            # Gaussian Fourier features embeddings.
            modules.append(GaussianFourierProjection(embedding_size=nf, scale=fourier_scale))
            embed_dim = 2 * nf

        elif embedding_type == "positional":
            embed_dim = nf

        else:
            raise ValueError(f"embedding type {embedding_type} unknown.")

        modules.append(nn.Linear(embed_dim, nf * 4))
        modules.append(nn.Linear(nf * 4, nf * 4))

        AttnBlock = functools.partial(AttentionBlock, overwrite_linear=True, rescale_output_factor=math.sqrt(2.0))

        if fir:
            Up_sample = functools.partial(FirUpsample2D, fir_kernel=fir_kernel, use_conv=resamp_with_conv)
        else:
            Up_sample = functools.partial(Upsample2D, name="Conv2d_0")

        if progressive == "output_skip":
            self.pyramid_upsample = Up_sample(channels=None, use_conv=False)
        elif progressive == "residual":
            pyramid_upsample = functools.partial(Up_sample, use_conv=True)

        if fir:
            Down_sample = functools.partial(FirDownsample2D, fir_kernel=fir_kernel, use_conv=resamp_with_conv)
        else:
            Down_sample = functools.partial(Downsample2D, padding=0, name="Conv2d_0")

        if progressive_input == "input_skip":
            self.pyramid_downsample = Down_sample(channels=None, use_conv=False)
        elif progressive_input == "residual":
            pyramid_downsample = functools.partial(Down_sample, use_conv=True)

        channels = num_channels
        if progressive_input != "none":
            input_pyramid_ch = channels

        modules.append(nn.Conv2d(channels, nf, kernel_size=3, padding=1))
        hs_c = [nf]

        in_ch = nf
        for i_level in range(self.num_resolutions):
            # Residual blocks for this resolution
            for i_block in range(num_res_blocks):
                out_ch = nf * ch_mult[i_level]
                modules.append(
                    ResnetBlock2D(
                        in_channels=in_ch,
                        out_channels=out_ch,
                        temb_channels=4 * nf,
                        output_scale_factor=np.sqrt(2.0),
                        non_linearity="silu",
                        groups=min(in_ch // 4, 32),
                        groups_out=min(out_ch // 4, 32),
                        overwrite_for_score_vde=True,
                    )
                )
                in_ch = out_ch

                if all_resolutions[i_level] in attn_resolutions:
                    modules.append(AttnBlock(channels=in_ch))
                hs_c.append(in_ch)

            if i_level != self.num_resolutions - 1:
                modules.append(
                    ResnetBlock2D(
                        in_channels=in_ch,
                        temb_channels=4 * nf,
                        output_scale_factor=np.sqrt(2.0),
                        non_linearity="silu",
                        groups=min(in_ch // 4, 32),
                        groups_out=min(out_ch // 4, 32),
                        overwrite_for_score_vde=True,
                        down=True,
                        kernel="fir" if fir else "sde_vp",
                        use_nin_shortcut=True,
                    )
                )

                if progressive_input == "input_skip":
                    modules.append(combiner(dim1=input_pyramid_ch, dim2=in_ch))
                    if combine_method == "cat":
                        in_ch *= 2

                elif progressive_input == "residual":
                    modules.append(pyramid_downsample(channels=input_pyramid_ch, out_channels=in_ch))
                    input_pyramid_ch = in_ch

                hs_c.append(in_ch)

        # mid
        self.mid = UNetMidBlock2D(
            in_channels=in_ch,
            temb_channels=4 * nf,
            output_scale_factor=math.sqrt(2.0),
            resnet_act_fn="silu",
            resnet_groups=min(in_ch // 4, 32),
            dropout=dropout,
        )

        in_ch = hs_c[-1]
        modules.append(
            ResnetBlock2D(
                in_channels=in_ch,
                temb_channels=4 * nf,
                output_scale_factor=np.sqrt(2.0),
                non_linearity="silu",
                groups=min(in_ch // 4, 32),
                groups_out=min(out_ch // 4, 32),
                overwrite_for_score_vde=True,
            )
        )
        modules.append(AttnBlock(channels=in_ch))
        modules.append(
            ResnetBlock2D(
                in_channels=in_ch,
                temb_channels=4 * nf,
                output_scale_factor=np.sqrt(2.0),
                non_linearity="silu",
                groups=min(in_ch // 4, 32),
                groups_out=min(out_ch // 4, 32),
                overwrite_for_score_vde=True,
            )
        )
        self.mid.resnets[0] = modules[len(modules) - 3]
        self.mid.attentions[0] = modules[len(modules) - 2]
        self.mid.resnets[1] = modules[len(modules) - 1]

        pyramid_ch = 0
        # Upsampling block
        for i_level in reversed(range(self.num_resolutions)):
            for i_block in range(num_res_blocks + 1):
                out_ch = nf * ch_mult[i_level]
                in_ch = in_ch + hs_c.pop()
                modules.append(
                    ResnetBlock2D(
                        in_channels=in_ch,
                        out_channels=out_ch,
                        temb_channels=4 * nf,
                        output_scale_factor=np.sqrt(2.0),
                        non_linearity="silu",
                        groups=min(in_ch // 4, 32),
                        groups_out=min(out_ch // 4, 32),
                        overwrite_for_score_vde=True,
                    )
                )
                in_ch = out_ch

            if all_resolutions[i_level] in attn_resolutions:
                modules.append(AttnBlock(channels=in_ch))

            if progressive != "none":
                if i_level == self.num_resolutions - 1:
                    if progressive == "output_skip":
                        modules.append(nn.GroupNorm(num_groups=min(in_ch // 4, 32), num_channels=in_ch, eps=1e-6))
                        modules.append(nn.Conv2d(in_ch, channels, kernel_size=3, padding=1))
                        pyramid_ch = channels
                    elif progressive == "residual":
                        modules.append(nn.GroupNorm(num_groups=min(in_ch // 4, 32), num_channels=in_ch, eps=1e-6))
                        modules.append(nn.Conv2d(in_ch, in_ch, bias=True, kernel_size=3, padding=1))
                        pyramid_ch = in_ch
                    else:
                        raise ValueError(f"{progressive} is not a valid name.")
                else:
                    if progressive == "output_skip":
                        modules.append(nn.GroupNorm(num_groups=min(in_ch // 4, 32), num_channels=in_ch, eps=1e-6))
                        modules.append(nn.Conv2d(in_ch, channels, bias=True, kernel_size=3, padding=1))
                        pyramid_ch = channels
                    elif progressive == "residual":
                        modules.append(pyramid_upsample(channels=pyramid_ch, out_channels=in_ch))
                        pyramid_ch = in_ch
                    else:
                        raise ValueError(f"{progressive} is not a valid name")

            if i_level != 0:
                modules.append(
                    ResnetBlock2D(
                        in_channels=in_ch,
                        temb_channels=4 * nf,
                        output_scale_factor=np.sqrt(2.0),
                        non_linearity="silu",
                        groups=min(in_ch // 4, 32),
                        groups_out=min(out_ch // 4, 32),
                        overwrite_for_score_vde=True,
                        up=True,
                        kernel="fir" if fir else "sde_vp",
                        use_nin_shortcut=True,
                    )
                )

        assert not hs_c

        if progressive != "output_skip":
            modules.append(nn.GroupNorm(num_groups=min(in_ch // 4, 32), num_channels=in_ch, eps=1e-6))
            modules.append(nn.Conv2d(in_ch, channels, kernel_size=3, padding=1))

        self.all_modules = nn.ModuleList(modules)

    def remove_ldm(self):
        del self.time_embed
        del self.input_blocks
        del self.middle_block
        del self.output_blocks
        del self.out

    def remove_ddpm(self):
        del self.temb
        del self.down
        del self.mid_new
        del self.up
        del self.norm_out


def nonlinearity(x):
    # swish
    return x * torch.sigmoid(x)


def Normalize(in_channels):
    return torch.nn.GroupNorm(num_groups=32, num_channels=in_channels, eps=1e-6, affine=True)
