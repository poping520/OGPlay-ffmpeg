# OGPlay FFmpeg 构建仓库

为 OGPlay 构建裁剪版 FFmpeg：把 avformat/avcodec/swscale/swresample/avutil 静态库合并为
单一共享库 `ogplay_ffmpeg`，只导出 FFmpeg 公共 API，产物发布到
[OGPlay-ffmpeg-prebuilt](../OGPlay-ffmpeg-prebuilt)。本仓库与 OGPlay 源码树完全独立，
OGPlay 只消费 prebuilt 仓库。

## 固定事实

全部写在 [`ffmpeg.json`](ffmpeg.json)，脚本不另存一份：

- 源码：`ffmpeg/` 浅 submodule，固定 tag `n8.1.3`（commit `1041abdc…`），脚本拒绝其他 commit
  或有本地修改的源码。
- 组件：demuxer `mov`（mp4/m4v/3gp/mov）；decoder `h264`、`mpeg4`、`aac`；parser `h264`、
  `mpeg4video`、`aac`。`h263` 解码器由 mpeg4 依赖自动带入，列在 `implied_components`。
  实际启用集合必须与“组件 + 隐含组件”**完全相等**，多一个少一个都构建失败。
- 无 protocol、encoder、muxer、filter、device、hwaccel、外部库；不含 GPL/nonfree 组件。
- `consumer_api`：OGPlay 实际调用的函数，构建时检查全部导出。

## 平台

| 平台 | 主机 | 依赖 |
| --- | --- | --- |
| `macos-arm64` | macOS（Apple Silicon） | Xcode 或 Command Line Tools、Python 3.9+ |
| `macos-x64` | macOS（交叉编译） | 同上，另需 `nasm` |
| `windows-x64` | Windows | Visual Studio（C++ x64 工具）、MSYS2（`pacman -S make nasm diffutils`）、Python 3.9+ |

macOS 上脚本一律经 `xcrun` 调用 Xcode 自带的 clang/ar/ranlib/nm/strip/otool/codesign：
Homebrew binutils 等 GNU 工具若排在 PATH 前面，会生成 Apple ld 拒绝的静态库并损坏
Mach-O。x86 目标强制 nasm，不允许静默退回无汇编的慢速 H.264。

Windows 上若当前环境没有 `cl`/`link`/`dumpbin`，脚本用 `vswhere` 找到最新 VS 并加载
`vcvars64.bat`；MSYS2 默认位于 `C:\msys64`，可用 `OGPLAY_MSYS2_ROOT` 指定。configure/make
在 MSYS2 bash 中继承 MSVC 环境运行（`--toolchain=msvc`，`/MD`），合并链接由原生
`link.exe` 按 `dumpbin` 生成的 `.def` 完成。

## 使用

```sh
git submodule update --init --depth 1
git -C ffmpeg fetch --depth 1 origin tag n8.1.3
python3 tools/build_ffmpeg.py all --platform macos-arm64
```

浅克隆可能只取得固定 commit，未取得对应 tag；构建前补取 `ffmpeg.json` 中的固定标签，
确保生成的版本头与 prebuilt 公共头一致。升级源码时同步替换上述标签。

Windows PowerShell（在本仓库根目录运行）：

```powershell
git submodule update --init --depth 1
git -C ffmpeg fetch --depth 1 origin tag n8.1.3
# MSYS2 不在默认 C:\msys64 时，设置实际安装路径。
$env:OGPLAY_MSYS2_ROOT = 'D:\01_software\msys64'
python tools/build_ffmpeg.py all --platform windows-x64
```

MSYS2 的依赖需先在其终端中安装：`pacman -S --needed make nasm diffutils`。

`all` 依次执行以下步骤，也可单独运行：

| 命令 | 作用 |
| --- | --- |
| `print-config --platform P` | 打印 configure 参数 |
| `build --platform P` | 清空并重建 `build/P/`：configure、make、组件检查、合并链接、导出/依赖检查 |
| `package --platform P` | 把 `build/P/stage` 写入 prebuilt 仓库：平台目录整体替换，公共头不一致时拒绝 |
| `smoke --platform P` | 用 prebuilt 中的包编译 `tests/smoke.c`，核对运行时组件并解码全部样本 |
| `verify` | 校验 prebuilt 仓库全部清单、文件哈希及源码 commit 一致性 |

`--prebuilt` 默认指向同级目录 `../OGPlay-ffmpeg-prebuilt`。升级 FFmpeg 源码导致公共头
变化时，`package` 需要 `--replace-headers`，并且所有平台都要重新构建。

## 构建时的机器检查

1. 源码 commit 与 `ffmpeg.json` 一致且无本地修改。
2. `config_components.h` 中启用的组件与配置完全相等。
3. 合并库的导出集合等于静态库中全部公共符号（`av_`/`avcodec_`/…前缀，排除
   `avpriv_`/`ff_` 私有符号），且包含全部 `consumer_api`。
4. 依赖只能是系统库：macOS 仅 `libSystem`；Windows 仅系统 DLL 与 MSVC 运行时。
5. 冒烟：运行时版本与头文件版本一致；运行时 decoder/demuxer 与配置一致；没有
   encoder/muxer/protocol；样本按 [`tests/samples/samples.json`](tests/samples/samples.json)
   判定帧数、尺寸、RGBA 转换、seek、PCM 样本数，未编入的编码必须报告 `unsupported`。

## 新增编码

只在 OGPlay 实际遇到未支持编码（运行时报告 `unsupported` 及编码名）时扩充：

1. 在 `ffmpeg.json` 的 `components` 中加入 decoder（及对应 parser）；若 configure 自动带入
   其他组件，构建会列出差异，确认后写入 `implied_components`。
2. 在 `tests/samples/` 用合成源生成小样本，加入 `samples.json`；若该编码原先充当
   `unsupported` 样本，换一个仍未编入的编码。
3. 在每个平台运行 `all`，把 prebuilt 仓库的变化一并提交。

## 许可证

产物为 LGPL-2.1-or-later 的 FFmpeg 动态库；每个平台包携带 FFmpeg 的
`COPYING.LGPLv2.1` 与 `LICENSE.md`，清单记录源码 tag、commit 与完整 configure 参数。
对应源码即本仓库 `ffmpeg/` submodule 固定的上游 commit 加本仓库脚本。
