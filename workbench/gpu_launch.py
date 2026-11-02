"""Deterministic GPU launch configuration and failure classification (no retries)."""


def image_lines(spec):
    if spec['program'] == 'custom':
        # Blender-style application ENTRYPOINTs must not consume Modal's Python argv.
        lines = [f"image = modal.Image.from_registry({spec['image']!r}, add_python='3.12').entrypoint([])",
                 "image = image.run_commands('python -c \"import sys; assert sys.version_info >= (3, 11)\"')"]
    else:
        lines = ["image = modal.Image.debian_slim(python_version='3.12')"]
    packages = list(spec['packages'])
    if spec['program'] == 'pytorch' and not any(p.split('=')[0] == 'torch' for p in packages):
        packages.append('torch')
    if packages:
        lines.append(f'image = image.pip_install(*{packages!r})')
    if spec['program'] == 'blender':
        lines += ["image = image.apt_install('curl', 'xz-utils', 'libxi6', 'libxrender1', 'libxfixes3', 'libgl1', 'libegl1', 'libsm6', 'libxkbcommon0', 'libgomp1')",
                  "image = image.run_commands('curl -fL --retry 0 https://download.blender.org/release/Blender4.5/blender-4.5.3-linux-x64.tar.xz -o /tmp/blender.tar.xz', 'mkdir -p /opt/blender && tar -xJf /tmp/blender.tar.xz -C /opt/blender --strip-components=1 && rm /tmp/blender.tar.xz')"]
    return lines


def classify_failure(logs, *, startup=False):
    value = logs.lower()
    category, code, reason = 'unknown', 'unclassified', 'Inspect the redacted local GPU log before changing configuration.'
    if 'cannot read file "/python"' in value:
        category, code, reason = 'image', 'image_entrypoint', 'Image ENTRYPOINT consumed the Modal Python runtime arguments.'
    elif any(x in value for x in ('exec format error', 'manifest unknown', 'pull access denied', 'failed to pull', 'python: not found', 'python: command not found', 'no module named modal', 'failed to build image')):
        category, code, reason = 'image', 'image_runtime_or_build', 'Image import/build or the required Python runtime failed.'
    elif any(x in value for x in ('cuda error', 'cuda driver', 'libcuda', 'no cuda', 'no compatible gpu', 'unsupported gpu', 'optix error')):
        category, code, reason = 'driver', 'gpu_driver_or_device', 'CUDA/driver/device initialization failed.'
    elif any(x in value for x in ('waiting to be scheduled', 'concurrency limits', 'no available gpu', 'resource exhausted', 'resource_exhausted', 'allocation timeout')):
        category, code, reason = 'allocation', 'gpu_allocation', 'GPU scheduling or workspace capacity was unavailable.'
    elif startup or any(x in value for x in ('failed during startup', 'crash-looping', 'runner failed with exit code')):
        category, code, reason = 'image', 'container_startup', 'The container failed before the payload started.'
    elif any(x in value for x in ('filenotfounderror', 'command not found', 'unrecognized argument', 'unknown argument', 'command exited', 'exit_code')):
        category, code, reason = 'command', 'program_failed', 'The requested program/argv failed; inspect run.log.'
    return {'category':category, 'code':code, 'phase':'startup' if startup else 'execution',
            'reason':reason, 'automatic_retry':False}


def startup_failure(line):
    value = line.lower()
    return any(x in value for x in ('failed during startup', 'runner failed with exit code', 'is crash-looping', 'cannot read file "/python"'))
