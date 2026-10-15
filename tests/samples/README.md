# 冒烟样本

全部由 FFmpeg 合成源（`testsrc2` 画面 + `sine` 音调）生成，不含任何游戏素材。期望结果见
`samples.json`，由 `tools/build_ffmpeg.py smoke` 判定。

| 文件 | 覆盖点 |
| --- | --- |
| `h264-high-aac.mp4` | H.264 High profile + B 帧重排，AAC-LC 立体声，faststart |
| `mpeg4-aac-moov-at-end.mp4` | MPEG-4 Part 2 Simple Profile，moov 在文件尾（custom AVIO seek） |
| `h263-aac.3gp` | 3GP 容器，H.263（mpeg4 解码器的隐含依赖） |
| `hevc-aac.mp4` | 未编入的编码：必须按名称报告 `unsupported`，不得崩溃或伪造成功 |

生成命令（FFmpeg 8.1，需 libx264/libx265）：

```sh
F() { ffmpeg -hide_banner -loglevel error -y "$@"; }
F -f lavfi -i testsrc2=s=160x90:r=12:d=2 -f lavfi -i sine=f=440:r=44100:d=2 \
  -c:v libx264 -profile:v high -bf 2 -g 12 -pix_fmt yuv420p \
  -c:a aac -b:a 64k -ac 2 -movflags +faststart h264-high-aac.mp4
F -f lavfi -i testsrc2=s=160x90:r=12:d=2 -f lavfi -i sine=f=660:r=22050:d=2 \
  -c:v mpeg4 -q:v 6 -g 12 -c:a aac -b:a 32k -ac 1 mpeg4-aac-moov-at-end.mp4
F -f lavfi -i testsrc2=s=176x144:r=12:d=2 -f lavfi -i sine=f=330:r=22050:d=2 \
  -c:v h263 -q:v 8 -g 12 -c:a aac -b:a 32k -ac 1 -f 3gp h263-aac.3gp
F -f lavfi -i testsrc2=s=160x96:r=12:d=1 -f lavfi -i sine=f=440:r=44100:d=1 \
  -c:v libx265 -x265-params log-level=error -tag:v hvc1 -pix_fmt yuv420p \
  -c:a aac -b:a 32k -ac 1 hevc-aac.mp4
```
