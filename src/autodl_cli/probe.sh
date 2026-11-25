#!/bin/bash
# AutoDL 实例硬件探测: 输出到 stdout, 本地存档
export PATH=/root/miniconda3/bin:$PATH
sec() { echo; echo "##### $*"; }
sec date; date
sec os; cat /etc/os-release | head -3; uname -a
sec nvidia-smi; nvidia-smi
sec nvidia-smi -q; nvidia-smi -q
sec proc-driver; cat /proc/driver/nvidia/version 2>/dev/null; cat /proc/driver/nvidia/gpus/*/information 2>/dev/null
sec lspci; (lspci -nn 2>/dev/null | grep -i -E 'nvidia|vga|3d') || echo "no lspci"
sec sysfs-pci; for d in /sys/bus/pci/devices/*; do v=$(cat $d/vendor 2>/dev/null); [ "$v" = "0x10de" ] && echo "$d vendor=$v device=$(cat $d/device) subsys=$(cat $d/subsystem_vendor):$(cat $d/subsystem_device) class=$(cat $d/class) link=$(cat $d/current_link_speed 2>/dev/null)x$(cat $d/current_link_width 2>/dev/null) max=$(cat $d/max_link_speed 2>/dev/null)x$(cat $d/max_link_width 2>/dev/null)"; done
sec topo; nvidia-smi topo -m 2>&1
sec query; nvidia-smi --query-gpu=name,pci.device_id,pci.sub_device_id,vbios_version,pcie.link.gen.max,pcie.link.gen.current,pcie.link.width.max,pcie.link.width.current,power.limit,power.default_limit,power.max_limit,clocks.max.sm,clocks.max.mem,memory.total,compute_cap,temperature.gpu,fan.speed --format=csv
sec cpu; lscpu | head -20; echo "nproc=$(nproc)"; cat /sys/fs/cgroup/cpu.max 2>/dev/null; cat /sys/fs/cgroup/cpu/cpu.cfs_quota_us 2>/dev/null
sec mem; free -g; cat /sys/fs/cgroup/memory.max 2>/dev/null; cat /sys/fs/cgroup/memory/memory.limit_in_bytes 2>/dev/null
sec disk; df -h / /root/autodl-tmp /root/autodl-fs 2>/dev/null; lsblk 2>/dev/null | head
sec net; cat /etc/pip.conf ~/.pip/pip.conf ~/.config/pip/pip.conf 2>/dev/null; ls /etc/network_turbo 2>/dev/null && echo turbo-available
for u in https://hf-mirror.com https://www.modelscope.cn https://huggingface.co https://github.com; do printf "%s " $u; curl -s -o /dev/null -m 8 -w "%{http_code} %{time_total}s\n" $u || echo fail; done
sec bench; nvidia-smi --query-gpu=power.draw,clocks.sm,clocks.mem,temperature.gpu --format=csv,noheader -lms 500 > /tmp/pw.log 2>&1 & PW=$!
timeout 60 python - <<'EOF'
import torch, time
d = torch.device("cuda")
p = torch.cuda.get_device_properties(0)
print("torch", torch.__version__, "|", p.name, f"cc{p.major}.{p.minor}", "SMs", p.multi_processor_count, f"{p.total_memory/2**30:.1f}GiB", "L2", getattr(p, "L2_cache_size", "?"))
def mm(dt, n=8192, it=30):
    a = torch.randn(n, n, device=d, dtype=dt); b = torch.randn(n, n, device=d, dtype=dt)
    for _ in range(3): a @ b
    torch.cuda.synchronize(); t = time.time()
    for _ in range(it): a @ b
    torch.cuda.synchronize(); return 2 * n**3 * it / (time.time() - t) / 1e12
print(f"matmul bf16 {mm(torch.bfloat16):.1f} TFLOPS")
print(f"matmul fp16 {mm(torch.float16):.1f} TFLOPS")
torch.backends.cuda.matmul.allow_tf32 = False
print(f"matmul fp32 {mm(torch.float32, 4096, 20):.1f} TFLOPS")
torch.backends.cuda.matmul.allow_tf32 = True
print(f"matmul tf32 {mm(torch.float32, 4096, 20):.1f} TFLOPS")
x = torch.empty(1024**3 // 2, device=d, dtype=torch.float32)  # 2 GiB
y = torch.empty_like(x)
for _ in range(3): y.copy_(x)
torch.cuda.synchronize(); t = time.time()
for _ in range(20): y.copy_(x)
torch.cuda.synchronize(); print(f"HBM/GDDR copy {2 * x.numel() * 4 * 20 / (time.time() - t) / 1e9:.0f} GB/s (read+write)")
h = torch.empty(512 * 1024**2, dtype=torch.uint8).pin_memory(); g = torch.empty_like(h, device=d)
for _ in range(2): g.copy_(h)
torch.cuda.synchronize(); t = time.time()
for _ in range(10): g.copy_(h, non_blocking=True)
torch.cuda.synchronize(); print(f"PCIe H2D {h.numel() * 10 / (time.time() - t) / 1e9:.1f} GB/s")
t = time.time()
for _ in range(10): h.copy_(g, non_blocking=True)
torch.cuda.synchronize(); print(f"PCIe D2H {h.numel() * 10 / (time.time() - t) / 1e9:.1f} GB/s")
EOF
kill $PW 2>/dev/null
sec power-under-load "(峰值功耗 / SM 频率采样)"; sort -t, -k1 -n -r /tmp/pw.log | head -3; echo "samples $(wc -l < /tmp/pw.log)"
