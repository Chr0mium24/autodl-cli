#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["requests"]
# ///
"""AutoDL 命令行: 行情 / 租卡 / 开关机 / SSH / 释放。走网页内部接口 www.autodl.com/api/v1/。

用法:
    autodl me                          # 余额、账号
    autodl market [4090] [--save] [--save-dir 目录]   # 全市场库存 + 单卡价(--save 存 JSON 快照)
    autodl machines "RTX 4090" [-n 1] [--region 西北]   # 有空闲卡的机器, 按价排序
    autodl images [pytorch]            # 基础镜像
    autodl create "RTX 4090" [-n 1] [--region ..] [--image base-image-..] [--machine id]
                  [--shutdown-at "2026-10-22 23:00" | --shutdown-in 3h] [--name x] [-y]
    autodl ls                          # 我的实例
    autodl info|wait|start|stop|release <实例>  # <实例> = uuid 前缀或名字; start --no-gpu 无卡开机
    autodl shutdown-at <实例> "2026-10-22 23:00"|3h|off
    autodl ssh <实例> [命令...]         # 自动带密码(sshpass), 无命令则交互
    autodl push <实例> <本地路径> [远端目录=/root/autodl-tmp]
    autodl pull <实例> <远端路径> [本地目录] [-j 8]   # 单文件切块并行下载 + md5 校验
    autodl probe <实例>                # 跑内置硬件探测脚本(nvidia-smi / PCIe / matmul / 带宽)

花钱的操作(create/start)会先打印价格并要求输入 y, -y 跳过; release 同理。
价格字段 payg_price 单位 元×1000/卡/小时; 钱包 assets 同口径。
token(网页登录后 localStorage 里的 token, 约 60 天有效)优先级:
    $AUTODL_TOKEN > 配置文件 token > 配置文件 token_cmd 的输出
配置文件 ~/.config/autodl/config.toml (或 $AUTODL_CONFIG):
    token = "eyJ..."                       # 二选一
    token_cmd = "some-cmd --print-token"   # 输出 token 的命令, 允许 `token: "..."` 格式
    save_dir = "~/autodl-market"           # market --save 默认目录(缺省为当前目录)
"""
import argparse
import json
import os
import pathlib
import re
import shlex
import subprocess
import sys
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import requests

API = "https://www.autodl.com/api/v1/"
CST = timezone(timedelta(hours=8))
_token = None


def config():
    path = pathlib.Path(os.environ.get("AUTODL_CONFIG") or "~/.config/autodl/config.toml").expanduser()
    if not path.exists():
        return {}
    try:
        return tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as e:
        sys.exit(f"配置文件 {path} 格式错误: {e}")


def token():
    global _token
    if _token:
        return _token
    cfg = config()
    tok = os.environ.get("AUTODL_TOKEN") or cfg.get("token")
    if not tok and cfg.get("token_cmd"):
        cmd = os.path.expanduser(cfg["token_cmd"])
        r = subprocess.run(shlex.split(cmd), capture_output=True, text=True)
        out = r.stdout.strip()
        tok = (out.split("token:", 1)[1] if out.startswith("token:") else out).strip().strip('"')
    if not tok:
        sys.exit("拿不到 token: 浏览器登录 autodl.com 后, 控制台执行 localStorage.token 取值,\n"
                 "  export AUTODL_TOKEN=... 或写进 ~/.config/autodl/config.toml (见 autodl -h)")
    _token = tok
    return _token


NO_RETRY = {"order/instance/create/payg", "instance/power_on"}  # 花钱的接口超时不重发, 免得下两单


def call(method, ep, body=None, params=None, ok=True):
    tries = 1 if ep in NO_RETRY else 4
    for n in range(tries):
        try:  # autodl.com 偶发 TLS 握手卡死, 短超时 + 重试
            r = requests.request(method, API + ep, json=body, params=params, timeout=(8, 20),
                                 headers={"Authorization": token(), "User-Agent": "Mozilla/5.0"})
            break
        except (requests.Timeout, requests.ConnectionError):
            if n == tries - 1:
                sys.exit(f"[{ep}] 网络超时" + ("; 可能已生效, 先 autodl ls 确认再重试" if tries == 1 else ""))
    r.raise_for_status()
    d = r.json()
    if d.get("code") == "AuthorizeFailed":  # ok=False 的调用也不能吞掉, 否则 market 只打出空表
        sys.exit(f"[{ep}] token 失效: {d.get('msg')}; 重新登录网页后再取 token")
    if ok and d.get("code") != "Success":
        sys.exit(f"[{ep}] {d.get('code')}: {d.get('msg')}")
    return d


def yuan(milli):
    return f"¥{milli / 1000:.2f}"


def confirm(msg, yes):
    print(msg)
    if yes:
        return
    if not sys.stdin.isatty():
        sys.exit("非交互环境, 加 -y 才会真的执行")
    if input("确认? [y/N] ").strip().lower() != "y":
        sys.exit("已取消")


def ver(s):
    return tuple(int(x) for x in re.findall(r"\d+", s or "")) or (0,)


# ---------- 行情 ----------

def regions():
    groups = {}
    for r in call("GET", "region/list")["data"]:
        groups.setdefault(r["data_center"], []).append(r["region_sign"])
    return list(groups.values())


def stock_of(signs):
    d = call("POST", "machine/region/gpu_type", {"region_sign_list": signs}, ok=False)
    if d.get("code") != "Success":  # 有的区组合起来查会 RequestParameterIsWrong, 拆开重试
        return [x for s in signs for x in stock_of([s])] if len(signs) > 1 else []
    return [(g, "+".join(signs), v["total_gpu_num"], v["idle_gpu_num"])
            for item in d["data"] for g, v in item.items()]


def rentable(m, num=1):
    # on_line 3/4/5/6 = 前端视为不可租(显示空闲 0, 下单报"主机存储库存已满"等), 必须排除
    return (m["payg"] and m["on_line"] not in (3, 4, 5, 6) and m["gpu_idle_num"] >= num
            and m["max_instance_num"] > m["binding_instance_num"])


def machine_list(gpu=None, region_signs=(), pages=100):
    out, page = [], 1
    while page <= pages:
        d = call("POST", "user/machine/list", {
            "charge_type": "payg", "region_sign": "", "region_sign_list": list(region_signs),
            "gpu_type_name": [gpu] if gpu else [], "machine_tag_name": [], "gpu_idle_num": 0,
            "mount_net_disk": False, "cpu_arch": [], "chip_corp": [],
            "instance_disk_size_order": "", "pay_price_order": "", "gpu_idle_type": "",
            "default_order": True, "page_index": page, "page_size": 100})["data"]
        out += d["list"] or []
        if page >= d.get("max_page", 1):
            break
        page += 1
    return out


def cmd_market(a):
    # 卡型名单来自各区汇总接口; 但它的 idle 数严重偏低(5090 报 1, 逐机器数有 200+),
    # 所以总量/空闲/价格都按卡型逐台机器翻页统计, 覆盖全部地区。
    groups = regions()
    with ThreadPoolExecutor(8) as ex:
        names = sorted({g for part in ex.map(stock_of, groups) for g, *_ in part})
    names = [g for g in names if not a.filter or a.filter.lower() in g.lower()]

    def stat(g):
        ms = [m for m in machine_list(g) if m["gpu_name"] == g and m["payg"]]
        regs = {}
        for m in ms:
            free = m["gpu_idle_num"] if rentable(m) else 0
            r = regs.setdefault(m["region_name"], [m["region_name"], 0, 0, None])
            r[1] += m["gpu_number"]; r[2] += free
            if free:
                r[3] = min(r[3] or 1e9, m["payg_price"])
        ps = [m["payg_price"] for m in ms]
        idle_ps = [r[3] for r in regs.values() if r[3]]
        return {"gpu": g, "total": sum(r[1] for r in regs.values()), "idle": sum(r[2] for r in regs.values()),
                "price_min": min(ps) / 1000 if ps else None, "price_max": max(ps) / 1000 if ps else None,
                "idle_min": min(idle_ps) / 1000 if idle_ps else None,
                "regions": sorted(regs.values(), key=lambda r: -r[2])}
    with ThreadPoolExecutor(8) as ex:
        items = list(ex.map(stat, names))
    agg = {d["gpu"]: d for d in items}
    items.sort(key=lambda x: (x["idle"] == 0, x["price_min"] or 999))
    print(f"{'卡型':<18}{'总量':>6}{'空闲':>6}  {'单卡价(元/h)':<12} {'有空卡最低':<8}  地区(空闲/总量,有空卡最低价)")
    for d in items:
        pr = "-" if d["price_min"] is None else f"{d['price_min']:.2f}" + (
            f"~{d['price_max']:.2f}" if d["price_max"] != d["price_min"] else "")
        im = f"{d['idle_min']:.2f}" if d["idle_min"] else "-"
        regs = ", ".join(f"{r[0]}({r[2]}/{r[1]}" + (f",{r[3] / 1000:.2f})" if r[3] else ")")
                         for r in d["regions"][:4])
        print(f"{d['gpu']:<18}{d['total']:>6}{d['idle']:>6}  {pr:<12} {im:<8}  {regs}")
    if a.save:
        outdir = pathlib.Path(a.save_dir or config().get("save_dir") or ".").expanduser()
        outdir.mkdir(parents=True, exist_ok=True)
        path = outdir / f"{datetime.now():%Y%m%d-%H%M}-market.json"
        path.write_text(json.dumps({"captured_at": datetime.now().isoformat(), "gpu_types": agg},
                                   ensure_ascii=False, indent=1))
        print(f"[saved] {path}")


def find_gpu_name(q):
    names = sorted({g for grp in regions() for g, *_ in stock_of(grp)})
    exact = [n for n in names if n.lower() == q.lower()]
    hits = exact or [n for n in names if q.lower() in n.lower()]
    if len(hits) != 1:
        sys.exit(f"卡型 '{q}' 匹配到 {hits or '无'}; 可选: {', '.join(names)}")
    return hits[0]


def candidates(gpu_q, num, region_q=None):
    gpu = find_gpu_name(gpu_q)
    ms = [m for m in machine_list(gpu) if m["gpu_name"] == gpu and rentable(m, num)
          and (not region_q or region_q in m["region_name"])]
    ms.sort(key=lambda m: (m["payg_price"], -m["gpu_idle_num"]))
    return gpu, ms


def cmd_machines(a):
    gpu, ms = candidates(a.gpu, a.num, a.region)
    print(f"{gpu}: {len(ms)} 台机器有 ≥{a.num} 张空闲卡")
    for m in ms[:a.limit]:
        print(f"  {m['machine_id']}  {m['region_name']:<8} 空闲{m['gpu_idle_num']}/{m['gpu_number']}  "
              f"{yuan(m['payg_price'])}/卡/h  CUDA≤{m['highest_cuda_version']}  "
              f"盘{m['max_instance_disk_size'] // 2**30}G")


# ---------- 镜像 ----------

def base_images():
    ims = call("POST", "image/get", {})["data"]
    return [i for i in ims if not i["is_off"] and i["chip_corp"] == "nvidia" and i["cpu_arch"] == "x86"]


def cmd_images(a):
    for i in sorted(base_images(), key=lambda i: (i["framework_name"], ver(i["framework_version"]))):
        s = f"{i['framework_name']} {i['framework_version']} py{i['python_version']} cuda{i['cuda_version']}"
        if not a.filter or a.filter.lower() in s.lower():
            print(f"  {i['uuid']}  {s}")


def default_image(max_cuda):
    ims = [i for i in base_images() if i["framework_name"] == "PyTorch" and not i["is_desktop"]
           and ver(i["cuda_version"]) <= ver(max_cuda)]
    if not ims:
        sys.exit(f"没有 CUDA≤{max_cuda} 的 PyTorch 基础镜像, 用 --image 指定")
    return max(ims, key=lambda i: ver(i["framework_version"]))


# ---------- 实例 ----------

def instances():
    return call("POST", "instance", {"page_index": 1, "page_size": 100})["data"]["list"] or []


def resolve(q):
    ins = instances()
    hits = [i for i in ins if i["uuid"] == q or i.get("name") == q] or \
           [i for i in ins if i["uuid"].startswith(q)]
    if len(hits) != 1:
        sys.exit(f"实例 '{q}' 匹配到 {len(hits)} 个; 现有: " +
                 ", ".join(f"{i['uuid']}({i.get('name') or '-'})" for i in ins))
    return hits[0]


def ssh_target(i):
    m = re.search(r"-p\s+(\d+)\s+(\S+@\S+)", i.get("ssh_command") or "")
    if not m:
        sys.exit(f"实例 {i['uuid']} 还没有 SSH 地址(状态 {i['status']})")
    return m.group(2), m.group(1)


def show(i):
    no_gpu = i.get("start_mode") == "non_gpu" and i["status"] == "running"
    price = "无卡模式 ¥0.10/h" if no_gpu else (yuan(i["payg_price"]) + "/h" if i.get("payg_price") else "")
    print(f"{i['uuid']}  {i.get('name') or '-'}  [{i['status']}]  {i.get('region_name', '')}  "
          f"{i.get('snapshot_gpu_alias_name') or i.get('gpu_name', '')}×{i.get('req_gpu_amount', '?')}  {price}")
    if i.get("ssh_command"):
        print(f"    {i['ssh_command'].strip()}    密码: {i.get('root_password', '')}")


def cmd_me(a):
    u = call("GET", "user/detail")["data"]
    w = call("GET", "wallet")["data"]
    print(f"{u.get('nickname') or u.get('username')}  手机 {u.get('phone')}  uid {u['id']}")
    print(f"余额 {yuan(w['assets'])}  代金券 {yuan(w['voucher_balance'])}  冻结 {yuan(w['blocked_asset'])}")
    # start_no_gpu_num 含义未确认: 2026-11-19 值为 0 时无卡开机照样成功, 不是剩余额度
    print(f"实例上限 {u.get('max_instance_num')}  start_no_gpu_num={u.get('start_no_gpu_num')}(非无卡额度)")


def cmd_ls(a):
    ins = instances()
    if not ins:
        print("(没有实例)")
    for i in ins:
        show(i)
        if a.json:
            print(json.dumps(i, ensure_ascii=False, indent=1))


def cmd_info(a):
    i = resolve(a.inst)
    show(i)
    if a.json:
        print(json.dumps(i, ensure_ascii=False, indent=1))


def parse_when(s):
    if s in ("off", "none"):
        return None
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([hm])", s)
    if m:
        dt = datetime.now(CST) + timedelta(**{"hours" if m[2] == "h" else "minutes": float(m[1])})
        return dt.strftime("%Y-%m-%d %H:%M")
    datetime.strptime(s, "%Y-%m-%d %H:%M")
    return s


def set_shutdown(uuid, when):
    call("POST", "instance/timed/shutdown", {"instance_uuid": uuid, "shutdown_at": when})
    print(f"定时关机: {when or '已取消'} (北京时间)")


def cmd_shutdown_at(a):
    set_shutdown(resolve(a.inst)["uuid"], parse_when(a.when))


def wait_running(uuid, timeout=600):
    t0, last = time.time(), None
    while time.time() - t0 < timeout:
        i = next((x for x in instances() if x["uuid"] == uuid), None)
        st = i and i["status"]
        if st != last:
            print(f"  [{int(time.time() - t0)}s] {st}", flush=True)
            last = st
        if st == "running":
            return i
        if st and ("fail" in st or "error" in st):
            sys.exit(f"实例进入 {st}")
        time.sleep(5)
    sys.exit("等待超时")


def cmd_wait(a):
    show(wait_running(resolve(a.inst)["uuid"], a.timeout))


def cmd_create(a):
    if a.machine:
        gpu = None
        ms = [m for m in machine_list() if m["machine_id"] == a.machine]
        if not ms:
            sys.exit(f"找不到机器 {a.machine}")
    else:
        gpu, ms = candidates(a.gpu, a.num, a.region)
        if not ms:
            sys.exit(f"{gpu} 现在没有 ≥{a.num} 张空闲卡的机器 (autodl market {a.gpu} 看库存)")
    def pick_image(m):
        if a.image:
            img = next((i for i in base_images() if i["uuid"] == a.image), None)
            if img:
                return (a.image, ""), f"{img['framework_name']} {img['framework_version']} cuda{img['cuda_version']}"
            return ("", a.image), f"私有镜像 {a.image}"
        img = default_image(m["highest_cuda_version"])
        return (img["uuid"], ""), (f"{img['framework_name']} {img['framework_version']} "
                                   f"py{img['python_version']} cuda{img['cuda_version']}")

    m = ms[0]
    _, img_desc = pick_image(m)
    w = call("GET", "wallet")["data"]
    when = parse_when(a.shutdown_at or a.shutdown_in) if (a.shutdown_at or a.shutdown_in) else None
    confirm(f"租: {m['gpu_name']} ×{a.num}  {m['region_name']}  机器 {m['machine_id']}\n"
            f"    按量 {yuan(m['payg_price'] * a.num)}/h (整点扣费)  镜像 {img_desc}\n"
            f"    余额 {yuan(w['assets'])}  定时关机 {when or '无'}", a.yes)
    before = {i["uuid"] for i in instances()}
    # 列表里有空卡不代表能下单(主机存储满等), 失败就顺延到同价或更便宜的下一台, 不涨价不再问
    for m in [x for x in ms if x["payg_price"] <= ms[0]["payg_price"]][:8]:
        (base_uuid, priv_uuid), img_desc = pick_image(m)
        body = {
            "instance_info": {
                "machine_id": m["machine_id"], "charge_type": "payg", "req_gpu_amount": a.num,
                "base_image_uuid": base_uuid, "private_image_uuid": priv_uuid, "reproduction_uuid": "",
                "cg_application_uuid": "",
                "cg_application_info": {"image_name": "", "current_version_id": 0, "current_version": "", "image_id": 0},
                "instance_name": a.name or "", "expand_data_disk": 0, "reproduction_id": 0,
                "copy_data_disk_after_clone": False},
            "price_info": {"coupon_id_list": [], "machine_id": m["machine_id"], "charge_type": "payg",
                           "duration": 1, "num": a.num, "expand_data_disk": 0}}
        d = call("POST", "order/instance/create/payg", body, ok=False)
        if d.get("code") == "Success":
            break
        print(f"  机器 {m['machine_id']}({m['region_name']}) 下单失败: {d.get('code')} {d.get('msg')}")
        if d.get("code") in ("TORecharge", "TORealName") or a.machine:
            sys.exit(1)
    else:
        sys.exit("同价位机器都下单失败, 换卡型或 autodl machines 手动挑 --machine")
    print(f"已下单: {json.dumps(d.get('data'), ensure_ascii=False)[:200]}")
    new = None
    for _ in range(24):
        new = next((i for i in instances() if i["uuid"] not in before), None)
        if new:
            break
        time.sleep(5)
    if not new:
        sys.exit("下单成功但 2 分钟内没在实例列表里看到新实例, autodl ls 看看")
    print(f"实例 {new['uuid']} 创建中…")
    if when:
        set_shutdown(new["uuid"], when)
    if not a.no_wait:
        show(wait_running(new["uuid"]))


def cmd_start(a):
    i = resolve(a.inst)
    if a.no_gpu:
        confirm(f"无卡模式开机 {i['uuid']} (约 ¥0.1/h, 不能用 GPU)", a.yes)
        d = call("POST", "instance/power_on", {"instance_uuid": i["uuid"], "payload": "non_gpu"})
    else:
        confirm(f"开机 {i['uuid']}  {yuan(i.get('payg_price', 0))}/h 整点扣费", a.yes)
        d = call("POST", "instance/power_on", {"instance_uuid": i["uuid"]}, ok=False)
        if d.get("code") != "Success":
            sys.exit(f"开机失败 {d.get('code')}: {d.get('msg')}")
    if not a.no_wait:
        show(wait_running(i["uuid"]))


def cmd_stop(a):
    i = resolve(a.inst)
    call("POST", "instance/power_off", {"instance_uuid": i["uuid"]})
    print(f"已关机 {i['uuid']} (按量实例关机后 GPU 释放, 再开可能没卡)")


def cmd_release(a):
    i = resolve(a.inst)
    confirm(f"释放 {i['uuid']} ({i.get('name') or '-'}): 系统盘和数据盘全部清除, 不可恢复", a.yes)
    if i["status"] != "shutdown":
        call("POST", "instance/power_off", {"instance_uuid": i["uuid"]})
        for _ in range(60):
            if resolve(i["uuid"])["status"] == "shutdown":
                break
            time.sleep(3)
    call("POST", "instance/release", {"instance_uuid": i["uuid"]})
    print(f"已释放 {i['uuid']}")


SSH_OPTS = ["-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
            "-o", "LogLevel=ERROR", "-o", "ServerAliveInterval=30"]


def ssh_base(i):
    target, port = ssh_target(i)
    env = dict(os.environ, SSHPASS=i.get("root_password", ""))
    return ["sshpass", "-e", "ssh", *SSH_OPTS, "-p", port, target], env


def cmd_ssh(a):
    i = resolve(a.inst)
    cmd, env = ssh_base(i)
    if not a.cmd:
        cmd.insert(cmd.index("ssh") + 1, "-t")
        os.execvpe("sshpass", cmd, env)
    # 非交互 ssh 不读 profile, conda 的 python/pip 不在 PATH 上
    remote = "export PATH=/root/miniconda3/bin:$PATH; " + " ".join(a.cmd)
    os.execvpe("sshpass", cmd + [remote], env)


def cmd_push(a):
    i = resolve(a.inst)
    target, port = ssh_target(i)
    env = dict(os.environ, SSHPASS=i.get("root_password", ""))
    sh = "ssh " + " ".join(SSH_OPTS) + f" -p {port}"
    src = a.src.rstrip("/") + ("/" if os.path.isdir(a.src) else "")
    dst = f"{target}:{a.dst.rstrip('/')}/{os.path.basename(a.src.rstrip('/'))}"
    os.execvpe("sshpass", ["sshpass", "-e", "rsync", "-az", "--progress", "-e", sh, src, dst], env)


def cmd_pull(a):
    """目录走 rsync; 单个文件切成 N 块并行 scp 再拼 + md5 校验。
    海外↔AutoDL 单连接下行实测 ~30KB/s, 8 路并行 ~270KB/s(连接级限速)。"""
    import concurrent.futures
    import hashlib
    import shlex
    import tempfile
    i = resolve(a.inst)
    target, port = ssh_target(i)
    env = dict(os.environ, SSHPASS=i.get("root_password", ""))
    sh = "ssh " + " ".join(SSH_OPTS) + f" -p {port}"
    ssh = ["sshpass", "-e", "ssh", *SSH_OPTS, "-p", port, target]
    os.makedirs(a.dst, exist_ok=True)
    q = shlex.quote(a.src)
    probe = subprocess.run(ssh + [f"test -f {q} && stat -c %s {q} && md5sum {q}"], env=env,
                           capture_output=True, text=True)
    if probe.returncode != 0 or a.parallel <= 1:
        os.execvpe("sshpass", ["sshpass", "-e", "rsync", "-az", "--progress", "-e", sh,
                               f"{target}:{a.src}", a.dst], env)
    size, md5 = int(probe.stdout.split()[0]), probe.stdout.split()[1]
    rdir = f"/tmp/autodl-pull-{os.getpid()}"
    subprocess.run(ssh + [f"mkdir -p {rdir} && split -n {a.parallel} -d -a 3 {q} {rdir}/p."], env=env, check=True)
    tmp = tempfile.mkdtemp(prefix="autodl-pull-")
    names = [f"p.{k:03d}" for k in range(a.parallel)]

    def get(n):
        for _ in range(3):
            r = subprocess.run(["sshpass", "-e", "scp", *SSH_OPTS, "-P", port, f"{target}:{rdir}/{n}", tmp],
                               env=env, capture_output=True)
            if r.returncode == 0:
                return n
        raise RuntimeError(f"{n} 下载失败: {r.stderr.decode()[-200:]}")
    t0 = time.time()
    print(f"{a.src}: {size / 1e6:.1f}MB, {a.parallel} 路并行…", flush=True)
    with concurrent.futures.ThreadPoolExecutor(a.parallel) as ex:
        for n in ex.map(get, names):
            print(f"  {n} ok ({time.time() - t0:.0f}s)", flush=True)
    out = os.path.join(a.dst, os.path.basename(a.src))
    h = hashlib.md5()
    with open(out, "wb") as f:
        for n in names:
            b = open(os.path.join(tmp, n), "rb").read()
            h.update(b)
            f.write(b)
    subprocess.run(ssh + [f"rm -rf {rdir}"], env=env)
    if h.hexdigest() != md5:
        sys.exit(f"md5 不一致: {h.hexdigest()} != {md5}")
    print(f"{out}  {size / 1e6:.1f}MB in {time.time() - t0:.0f}s ({size / 1e3 / max(1, time.time() - t0):.0f}KB/s), md5 ok")


def cmd_probe(a):
    """把包里的 probe.sh 喂给远端 bash; 输出走 stdout, 自己重定向存档。"""
    cmd, env = ssh_base(resolve(a.inst))
    script = pathlib.Path(__file__).resolve().with_name("probe.sh")
    with open(script, "rb") as f:
        sys.exit(subprocess.run(cmd + ["bash -s"], stdin=f, env=env).returncode)


def main():
    if pathlib.Path(sys.argv[0]).name == "autodl-gpu":  # 旧入口兼容
        sys.argv.insert(1, "market")
    p = argparse.ArgumentParser(prog="autodl", description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd_name", required=True)

    def add(name, fn, *args):
        s = sub.add_parser(name)
        for spec in args:
            s.add_argument(*spec[0], **spec[1])
        s.set_defaults(fn=fn)
        return s

    inst = (["inst"], {"help": "uuid 前缀或实例名"})
    yes = (["-y", "--yes"], {"action": "store_true"})
    nowait = (["--no-wait"], {"action": "store_true"})
    add("me", cmd_me)
    add("market", cmd_market, (["filter"], {"nargs": "?"}), (["--save"], {"action": "store_true"}),
        (["--save-dir"], {"help": "默认取配置 save_dir, 否则当前目录"}))
    add("machines", cmd_machines, (["gpu"], {}), (["-n", "--num"], {"type": int, "default": 1}),
        (["--region"], {}), (["--limit"], {"type": int, "default": 15}))
    add("images", cmd_images, (["filter"], {"nargs": "?"}))
    add("create", cmd_create, (["gpu"], {"nargs": "?"}), (["-n", "--num"], {"type": int, "default": 1}),
        (["--region"], {"help": "地区名子串, 如 西北/北京"}), (["--image"], {}), (["--machine"], {}),
        (["--name"], {}), (["--shutdown-at"], {}), (["--shutdown-in"], {"help": "如 3h / 90m"}), yes, nowait)
    add("ls", cmd_ls, (["--json"], {"action": "store_true"}))
    add("info", cmd_info, inst, (["--json"], {"action": "store_true"}))
    add("wait", cmd_wait, inst, (["--timeout"], {"type": int, "default": 600}))
    add("start", cmd_start, inst, (["--no-gpu"], {"action": "store_true"}), yes, nowait)
    add("stop", cmd_stop, inst)
    add("release", cmd_release, inst, yes)
    add("shutdown-at", cmd_shutdown_at, inst, (["when"], {"help": "'YYYY-MM-DD HH:MM' / 3h / 90m / off"}))
    add("ssh", cmd_ssh, inst, (["cmd"], {"nargs": argparse.REMAINDER}))
    add("push", cmd_push, inst, (["src"], {}), (["dst"], {"nargs": "?", "default": "/root/autodl-tmp"}))
    add("pull", cmd_pull, inst, (["src"], {"help": "远端路径(单文件并行分块, 目录 rsync)"}),
        (["dst"], {"nargs": "?", "default": "."}), (["-j", "--parallel"], {"type": int, "default": 8}))
    add("probe", cmd_probe, inst)
    a = p.parse_args()
    if a.cmd_name == "create" and not (a.gpu or a.machine):
        p.error("create 需要卡型或 --machine")
    a.fn(a)


if __name__ == "__main__":
    main()
