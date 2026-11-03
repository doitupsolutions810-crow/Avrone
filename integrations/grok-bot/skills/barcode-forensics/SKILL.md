---
name: barcode-forensics
description: Decode and analyze QR codes and other 2D/1D barcodes in an image, then verify what the payload claims.
---
1. image_inspect the file: size, entropy, estimated module grid. A QR's side in modules = 17 + 4*version.
2. image_decode it. If zxing-cpp is missing, env_install it and retry.
3. If nothing decodes: try crops of each quadrant, thresholding, or inverting (python + Pillow).
4. Compare payload length with symbol capacity. A huge symbol with a tiny payload means padding,
   a low error-correction level mismatch, extra structured-append parts, or hidden data in the modules.
5. Parse the payload (JSON? URL? base64?). For every field, check whether it can be verified:
   recompute checksums (try CRC32, CRC64, FNV-1a, xxhash over plausible inputs), test seeds, look up names.
6. Report: what decoded, exact text, what is verifiable, what is just a claim, and what could not be checked.
Never run or open payload URLs blindly; fetch them read-only if needed.
