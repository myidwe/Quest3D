"""Compile and execute bundled sm75 CUDA kernels with tiny synthetic tensors."""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import json
from pathlib import Path
import subprocess
import sys


def verify_gpu() -> dict:
    import torch
    from quest3d.tonemap_cuda import CudaToneMapper
    from quest3d.depth_edges_cuda import CudaDepthEdges
    from quest3d.forward_warp_cuda import CudaForwardWarper
    from quest3d.stereo_pack_cuda import CudaStereoPacker
    from quest3d.resample import ReferenceCubicResize
    from quest3d.resample_cuda import CudaReferenceCubicResize
    from quest3d.colour_fit import fit_bgra_float
    from quest3d.colour_fit_cuda import CudaFloatColourFit

    if torch.__version__ != "2.7.1+cu126" or not torch.cuda.is_available():
        raise RuntimeError("Pinned Torch 2.7.1+cu126 and an available NVIDIA CUDA driver are required")
    capability = torch.cuda.get_device_capability(0)
    if capability != (7, 5):
        raise RuntimeError(f"This preview supports NVIDIA Turing sm75 (capability 7.5) only; found {capability}. Other GPU architectures require a separately validated implementation.")
    with ExitStack() as stack:
        tone = CudaToneMapper()
        stack.callback(tone.close)
        edges = CudaDepthEdges()
        stack.callback(edges.close)
        warp = CudaForwardWarper()
        stack.callback(warp.close)
        resize = CudaReferenceCubicResize()
        stack.callback(resize.close)
        colour_fit = stack.enter_context(CudaFloatColourFit())
        hdr = torch.full((32, 64, 4), 0.25, dtype=torch.float16, device="cuda")
        mapped = tone(hdr, sdr_white_scale=1.0)
        if mapped.shape != hdr.shape or mapped.dtype != torch.uint8:
            raise RuntimeError("CUDA tone mapper returned an invalid result")
        resize_source = torch.arange(32 * 64 * 4, device="cuda").remainder(256).to(torch.uint8).reshape(32, 64, 4)[:, :, :3]
        expected = ReferenceCubicResize()(resize_source, 29, 17)
        resized = resize(resize_source, 29, 17)
        if not torch.equal(expected.view(torch.int32), resized.view(torch.int32)):
            raise RuntimeError("Fused cubic resize differs from its reference")
        colour_source = torch.arange(32 * 64 * 4, device="cuda").remainder(256).byte().reshape(32, 64, 4)
        expected_colour = fit_bgra_float(colour_source, 50, 36)
        fitted_colour = colour_fit(colour_source, 50, 36)
        if (fitted_colour.content_rect != expected_colour.content_rect or not
                torch.equal(fitted_colour.image.view(torch.int32), expected_colour.image.view(torch.int32))):
            raise RuntimeError("Fused colour fit differs from its reference")
        rgb = torch.linspace(0, 1, 32 * 64, device="cuda").reshape(1, 1, 32, 64).expand(1, 3, 32, 64).contiguous()
        low_depth = torch.linspace(0.1, 0.9, 8 * 16, device="cuda").reshape(1, 8, 16)
        depth = edges(rgb, low_depth)
        stereo = warp(rgb, depth, 2.0, 0.5)
        packed_warp = stack.enter_context(CudaForwardWarper(fused_fill=True))
        packer = stack.enter_context(CudaStereoPacker())
        packed = packed_warp.packed(rgb, depth, 2.0, 0.5, 68, 36, (2, 2, 64, 32), fused_validation=True)
        expected_packed = packer(stereo.eyes, 68, 36, (2, 2, 64, 32))
        if not torch.equal(packed.cpu_bgra, expected_packed):
            raise RuntimeError("Fused final stereo output differs from the reference")
        torch.cuda.synchronize()
        if depth.shape != (32, 64) or stereo.eyes.shape != (2, 3, 32, 64) or not bool(torch.isfinite(depth).all() & torch.isfinite(stereo.eyes).all()):
            raise RuntimeError("CUDA depth/stereo kernel result is invalid")
        result = {"torch": torch.__version__, "device": torch.cuda.get_device_name(0),
                  "capability": list(capability), "nvrtc_kernels_executed": ["tone_map", "depth_edges", "forward_warp", "reference_cubic", "forward_fill_pack", "forward_validation", "colour_fit"],
                  "input": "tiny synthetic tensors; no screen capture or quality/FPS measurement",
                  "verified": True}
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.worker:
        print(json.dumps(verify_gpu(), ensure_ascii=True))
        return
    try:
        checked = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--worker"],
                                 capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    except subprocess.TimeoutExpired as error:
        raise RuntimeError("CUDA runtime validation exceeded 60 seconds") from error
    if checked.returncode:
        raise RuntimeError("CUDA runtime validation failed: " + checked.stderr[-12000:])
    result = json.loads(checked.stdout)
    output = json.dumps(result, indent=2, ensure_ascii=True) + "\n"
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(output, "utf-8")
    print(output)


if __name__ == "__main__":
    main()
