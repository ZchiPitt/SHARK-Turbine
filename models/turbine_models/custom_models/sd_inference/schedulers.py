# Copyright 2024 Advanced Micro Devices, Inc
#
# Licensed under the Apache License v2.0 with LLVM Exceptions.
# See https://llvm.org/LICENSE.txt for license information.
# SPDX-License-Identifier: Apache-2.0 WITH LLVM-exception

import os
from typing import List

import torch
from shark_turbine.aot import *
from iree.compiler.ir import Context
import iree.runtime as ireert
import numpy as np

from diffusers import (
    LCMScheduler,
    LMSDiscreteScheduler,
    PNDMScheduler,
    DDPMScheduler,
    DPMSolverSDEScheduler,
    DDIMScheduler,
    DPMSolverMultistepScheduler,
    KDPM2DiscreteScheduler,
    EulerDiscreteScheduler,
    EulerAncestralDiscreteScheduler,
    DEISMultistepScheduler,
    DPMSolverSinglestepScheduler,
    KDPM2AncestralDiscreteScheduler,
    HeunDiscreteScheduler,
)

from turbine_models.turbine_tank import turbine_tank
from turbine_models.custom_models.sd_inference import utils
from turbine_models.model_runner import vmfbRunner


class SharkSchedulerWrapper:
    def __init__(self, rt_device, vmfb):
        self.runner = vmfbRunner(rt_device, vmfb, None)

    def initialize(self, sample):
        return self.runner.ctx.modules.compiled_scheduler["run_initialize"](sample)

    def scale_model_input(self, sample, t):
        return self.runner.ctx.modules.compiled_scheduler["run_scale"](sample, t)

    def step(self, noise_pred, t, sample, guidance_scale, step_index):
        return self.runner.ctx.modules.compiled_scheduler["run_step"](
            noise_pred, t, sample, guidance_scale, step_index
        )


class SchedulingModel(torch.nn.Module):
    def __init__(
        self,
        hf_model_name,
        scheduler,
        height,
        width,
        batch_size,
        num_inference_steps,
        dtype,
    ):
        super().__init__()
        # For now, assumes SDXL implementation. May not need parametrization for other models,
        # but keeping hf_model_name in case.
        self.model = scheduler
        self.height = height
        self.width = width
        self.batch_size = batch_size
        self.do_classifier_free_guidance = True
        self.model.set_timesteps(num_inference_steps)
        self.model.is_scale_input_called = True
        self.dtype = dtype

    def initialize(self, sample):
        height = self.height
        width = self.width
        original_size = (height, width)
        target_size = (height, width)
        crops_coords_top_left = (0, 0)
        add_time_ids = list(original_size + crops_coords_top_left + target_size)
        add_time_ids = torch.tensor([add_time_ids], dtype=self.dtype)
        if self.do_classifier_free_guidance:
            add_time_ids = torch.cat([add_time_ids] * 2, dim=0)
            add_time_ids = add_time_ids.repeat(self.batch_size, 1).type(self.dtype)
        timesteps = self.model.timesteps
        step_indexes = torch.tensor(len(timesteps))
        sample = sample * self.model.init_noise_sigma
        return sample.type(self.dtype), add_time_ids, step_indexes

    def prepare_model_input(self, sample, t):
        t = self.model.timesteps[t]
        if self.do_classifier_free_guidance:
            latent_model_input = torch.cat([sample] * 2)
        else:
            latent_model_input = sample
        return self.model.scale_model_input(latent_model_input, t), t.type(self.dtype)

    def step(self, noise_pred, t, sample, guidance_scale, i):
        self.model._step_index = i
        if self.do_classifier_free_guidance:
            noise_preds = noise_pred.chunk(2)
            noise_pred = noise_preds[0] + guidance_scale * (
                noise_preds[1] - noise_preds[0]
            )
        sample = self.model.step(noise_pred, t, sample)[0]
        return sample.type(self.dtype)


class SharkSchedulerCPUWrapper:
    def __init__(self, pipe, scheduler):
        self.module = scheduler
        self.dest = pipe.runners["unet"].config.device
        self.dtype = pipe.iree_dtype

    def initialize(self, sample):
        sample, add_time_ids, step_indexes = self.module.initialize(
            torch.from_numpy(sample.to_host())
        )
        sample = ireert.asdevicearray(self.dest, sample, self.dtype)
        add_time_ids = ireert.asdevicearray(self.dest, add_time_ids, self.dtype)

        return sample, add_time_ids, step_indexes

    def scale_model_input(self, sample, t):
        scaled = ireert.asdevicearray(
            self.dest,
            self.module.scale_model_input(torch.from_numpy(sample.to_host()), t),
            self.dtype,
        )
        t = [self.module.model.timesteps[t]]
        t = ireert.asdevicearray(self.dest, t, self.dtype)
        return scaled, t

    def step(self, latents, t, sample):
        return ireert.asdevicearray(
            self.dest,
            self.module.step(
                torch.from_numpy(latents.to_host()),
                t,
                torch.from_numpy(sample.to_host()),
            ).prev_sample,
            self.dtype,
        )


def export_scheduler_model(
    hf_model_name: str,
    scheduler_id: str,
    batch_size: int = 1,
    height: int = 512,
    width: int = 512,
    num_inference_steps: int = 30,
    precision: str = "fp16",
    compile_to: str = "torch",
    device: str = None,
    target_triple: str = None,
    ireec_flags: str = None,
    exit_on_vmfb: bool = False,
    pipeline_dir: str = None,
    input_mlir: str = None,
    upload_ir=False,
):
    dtype = torch.float16 if precision == "fp16" else torch.float32
    scheduler = get_scheduler(hf_model_name, scheduler_id)
    scheduler_module = SchedulingModel(
        hf_model_name, scheduler, height, width, batch_size, num_inference_steps, dtype
    )
    vmfb_names = [
        scheduler_id + "Scheduler",
        f"bs{batch_size}",
        f"{height}x{width}",
        precision,
        str(num_inference_steps),
        target_triple,
    ]
    vmfb_name = "_".join(vmfb_names)

    if pipeline_dir:
        safe_name = os.path.join(pipeline_dir, vmfb_name)
    else:
        safe_name = utils.create_safe_name(hf_model_name, "_" + vmfb_name)

    if input_mlir:
        vmfb_path = utils.compile_to_vmfb(
            input_mlir,
            device,
            target_triple,
            ireec_flags,
            safe_name,
            mlir_source="file",
            return_path=not exit_on_vmfb,
        )
        return vmfb_path

    do_classifier_free_guidance = True
    if do_classifier_free_guidance:
        init_batch_dim = 2
    else:
        init_batch_dim = 1

    sample = (
        batch_size,
        4,
        height // 8,
        width // 8,
    )
    noise_pred_shape = (
        batch_size * init_batch_dim,
        4,
        height // 8,
        width // 8,
    )
    example_init_args = [torch.empty(sample, dtype=dtype)]
    example_prep_args = [
        torch.empty(sample, dtype=dtype),
        torch.empty(1, dtype=torch.int64),
    ]
    example_step_args = [
        torch.empty(noise_pred_shape, dtype=dtype),
        torch.empty(1, dtype=dtype),
        torch.empty(sample, dtype=dtype),
        torch.empty(1, dtype=dtype),
        torch.empty(1, dtype=torch.int64),
    ]

    fxb = FxProgramsBuilder(scheduler_module)

    @fxb.export_program(
        args=(example_init_args,),
    )
    def _initialize(module, sample):
        return module.initialize(*sample)

    @fxb.export_program(
        args=(example_prep_args,),
    )
    def _scale(module, input):
        return module.prepare_model_input(*input)

    @fxb.export_program(
        args=(example_step_args,),
    )
    def _step(module, inputs):
        return module.step(*inputs)

    decomp_list = []
    # if decomp_attn == True:
    #     decomp_list.extend(
    #         [
    #             torch.ops.aten._scaled_dot_product_flash_attention_for_cpu,
    #             torch.ops.aten._scaled_dot_product_flash_attention.default,
    #         ]
    #     )
    with decompositions.extend_aot_decompositions(
        from_current=True,
        add_ops=decomp_list,
    ):

        class CompiledScheduler(CompiledModule):
            run_initialize = _initialize
            run_scale = _scale
            run_step = _step

    import_to = "INPUT" if compile_to == "linalg" else "IMPORT"
    inst = CompiledScheduler(context=Context(), import_to=import_to)

    module_str = str(CompiledModule.get_mlir_module(inst))

    if compile_to != "vmfb":
        return module_str
    elif compile_to == "vmfb":
        vmfb = utils.compile_to_vmfb(
            module_str,
            device,
            target_triple,
            ireec_flags,
            safe_name,
            return_path=True,
        )
        if exit_on_vmfb:
            exit()
        return vmfb


def get_scheduler(model_id, scheduler_id):
    # TODO: switch over to turbine and run all on GPU
    print(f"\n[LOG] Initializing schedulers from model id: {model_id}")
    if scheduler_id in SCHEDULER_MAP.keys():
        scheduler = SCHEDULER_MAP[scheduler_id].from_pretrained(
            model_id, subfolder="scheduler"
        )
    return scheduler


SCHEDULER_MAP = {
    "PNDM": PNDMScheduler,
    "DDPM": DDPMScheduler,
    "KDPM2Discrete": KDPM2DiscreteScheduler,
    "LMSDiscrete": LMSDiscreteScheduler,
    "DDIM": DDIMScheduler,
    "LCMScheduler": LCMScheduler,
    "EulerDiscrete": EulerDiscreteScheduler,
    "EulerAncestralDiscrete": EulerAncestralDiscreteScheduler,
    "DEISMultistep": DEISMultistepScheduler,
    "DPMSolverSinglestep": DPMSolverSinglestepScheduler,
    "KDPM2AncestralDiscrete": KDPM2AncestralDiscreteScheduler,
    "HeunDiscrete": HeunDiscreteScheduler,
    "DPMSolverMultistepKarras": DPMSolverMultistepScheduler,
}

if __name__ == "__main__":
    from turbine_models.custom_models.sd_inference.sd_cmd_opts import args

    mod_str = export_scheduler_model(
        args.hf_model_name,
        args.scheduler_id,
        args.batch_size,
        args.height,
        args.width,
        args.num_inference_steps,
        args.precision,
        args.compile_to,
        args.device,
        args.iree_target_triple,
        args.ireec_flags,
        exit_on_vmfb=False,
        input_mlir=args.input_mlir,
    )
    vmfb_names = [
        args.scheduler_id + "Scheduler",
        f"_bs{args.batch_size}_{args.height}x{args.width}",
        args.precision,
        str(args.num_inference_steps),
        args.iree_target_triple,
    ]
    safe_name = "_".join(vmfb_names)
    if args.compile_to != "vmfb":
        with open(f"{safe_name}.mlir", "w+") as f:
            f.write(mod_str)
        print("Saved to", safe_name + ".mlir")
