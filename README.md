# autodl-cli

[AutoDL](https://www.autodl.com) 算力云的非官方命令行：查全市场库存和单卡价、按量租卡、开关机（含无卡模式）、定时关机、SSH 免密码登录、rsync / 并行分块传输、释放实例。

AutoDL 的官方开发者 API 只覆盖余额、NFS、Pro 实例和弹性部署，普通按量容器实例没有官方接口，所以本工具调用的是网页前端用的内部接口 `www.autodl.com/api/v1/`。接口随时可能变，出问题欢迎提 issue。

## 安装

```bash
uv tool install git+https://github.com/Chr0mium24/autodl-cli
```

也可以不装，直接跑单文件（PEP 723 脚本，uv 自动装依赖）：

```bash
uv run src/autodl_cli/cli.py me
```

`ssh` / `push` / `pull` / `probe` 需要本机有 `sshpass` 和 `rsync`（macOS：`brew install sshpass`）。

## 登录态

用的是网页登录后存在浏览器 localStorage 里的 JWT，有效期约 60 天。浏览器登录 autodl.com 后，在开发者工具控制台执行 `localStorage.token` 即可拿到。

按以下顺序读取：

1. 环境变量 `AUTODL_TOKEN`
2. 配置文件 `~/.config/autodl/config.toml`（可用 `AUTODL_CONFIG` 改路径）里的 `token`
3. 配置文件里 `token_cmd` 命令的输出（适合接自己的浏览器登录态读取脚本，输出纯 token 或 `token: "..."` 都行）

```toml
# ~/.config/autodl/config.toml
token = "eyJ..."
# token_cmd = "my-token-dumper autodl.com"
save_dir = "~/autodl-market"   # market --save 的默认目录，缺省为当前目录
```

token 过期时会报 `AuthorizeFailed: 登录超时`，重新登录网页取一次即可。

## 用法

```bash
autodl me                                  # 余额、代金券、实例上限
autodl market                              # 全部卡型：总量 / 空闲 / 单卡价 / 各地区库存
autodl market 4090 --save                  # 只看 4090，并存一份 JSON 快照
autodl machines "RTX 4090" -n 2 --region 西北   # 有 ≥2 张空卡的机器，按价格排序
autodl images pytorch                      # 基础镜像列表

autodl create "RTX 4090" --shutdown-in 3h --name exp1   # 租最便宜的一台，3 小时后自动关机
autodl create --machine <machine_id> --image <镜像uuid>  # 指定机器 / 镜像（也支持私有镜像）
autodl ls                                  # 我的实例（含 SSH 命令和密码）
autodl info exp1 --json                    # <实例> 写 uuid 前缀或实例名都行
autodl wait exp1                           # 等到 running
autodl start exp1 [--no-gpu]               # 开机；--no-gpu 无卡模式（约 ¥0.1/h，适合配环境、下数据）
autodl stop exp1
autodl shutdown-at exp1 "2026-10-22 23:00" # 或 3h / 90m / off（北京时间）
autodl release exp1                        # 释放：数据全部清除

autodl ssh exp1                            # 交互登录，自动填密码
autodl ssh exp1 nvidia-smi                 # 远程执行（已把 miniconda 加进 PATH）
autodl push exp1 ./data                    # rsync 到 /root/autodl-tmp/data
autodl pull exp1 /root/autodl-tmp/ckpt.pt -j 8   # 单文件切 8 块并行 scp + md5 校验；目录走 rsync
autodl probe exp1 > probe.txt              # 硬件探测：nvidia-smi、PCIe 链路、matmul TFLOPS、显存/PCIe 带宽、满载功耗
```

## 几点说明

- **花钱的操作要确认**：`create` / `start` / `release` 会先打印价格、余额等信息，输入 `y` 才执行；`-y` 跳过。非交互环境下不加 `-y` 直接拒绝。
- **下单不重试**：下单和开机接口超时不会自动重发，免得重复扣费；超时后先 `autodl ls` 看是否已生效。
- **下单失败自动顺延**：市场列表显示有空卡不代表一定能下单（比如主机存储已满），失败时会依次试同价或更便宜的下一台机器，最多 8 台，不会换到更贵的。
- **库存统计**：`market` 不用各区汇总接口的空闲数（明显偏低），而是按卡型逐台机器翻页统计；`on_line` 状态 3/4/5/6 的机器网页上显示不可租，这里也排除。
- **价格单位**：接口里的 `payg_price` 和钱包余额单位都是「元 × 1000」，输出时已换算成元。
- **并行下载**：从海外连 AutoDL 时，单连接下行实测只有约 30 KB/s，8 路并行约 270 KB/s（限速是按连接算的），所以 `pull` 对单文件默认切块并行。

## License

MIT
