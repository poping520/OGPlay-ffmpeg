/* Smoke test for a packaged OGPlay FFmpeg SDK.
 *
 *   smoke --list     print runtime/header versions and built-in components
 *   smoke <file>     demux and decode <file>, then print one JSON result line
 *
 * The SDK contains no protocols, so media is fed through a custom AVIOContext,
 * the same way OGPlay hands VFS data to FFmpeg. Results are judged by
 * tools/build_ffmpeg.py against tests/samples/samples.json. */

#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include <libavcodec/avcodec.h>
#include <libavformat/avformat.h>
#include <libavutil/channel_layout.h>
#include <libavutil/mem.h>
#include <libswresample/swresample.h>
#include <libswscale/swscale.h>

#define IO_BUFFER_SIZE 32768

typedef struct Memory {
    uint8_t *data;
    int64_t size;
    int64_t pos;
} Memory;

typedef struct Stats {
    int video_frames;
    int width;
    int height;
    int rgba_converted;
    int64_t audio_samples;
} Stats;

typedef struct Decoder {
    AVCodecContext *ctx;
    struct SwsContext *sws;
    SwrContext *swr;
    uint8_t *rgba;
} Decoder;

static int Fail(const char *what, int err) {
    char text[AV_ERROR_MAX_STRING_SIZE] = {0};
    av_strerror(err, text, sizeof text);
    fprintf(stderr, "FAIL %s: %s\n", what, text);
    return 1;
}

static int ReadPacket(void *opaque, uint8_t *dst, int size) {
    Memory *m = opaque;
    int64_t left = m->size - m->pos;
    if (left <= 0) return AVERROR_EOF;
    if (size > left) size = (int)left;
    memcpy(dst, m->data + m->pos, (size_t)size);
    m->pos += size;
    return size;
}

static int64_t Seek(void *opaque, int64_t offset, int whence) {
    Memory *m = opaque;
    int64_t base;
    whence &= ~AVSEEK_FORCE;
    if (whence == AVSEEK_SIZE) return m->size;
    if (whence == SEEK_SET) base = 0;
    else if (whence == SEEK_CUR) base = m->pos;
    else if (whence == SEEK_END) base = m->size;
    else return AVERROR(EINVAL);
    if (base + offset < 0 || base + offset > m->size) return AVERROR(EINVAL);
    m->pos = base + offset;
    return m->pos;
}

static int LoadFile(const char *path, Memory *m) {
    FILE *file = fopen(path, "rb");
    if (!file) return 0;
    fseek(file, 0, SEEK_END);
    m->size = ftell(file);
    fseek(file, 0, SEEK_SET);
    m->data = malloc((size_t)m->size);
    m->pos = 0;
    int ok = m->data && fread(m->data, 1, (size_t)m->size, file) == (size_t)m->size;
    fclose(file);
    return ok;
}

static void PrintNames(const char *key, const char **names, int count, int last) {
    printf("\"%s\":[", key);
    for (int i = 0; i < count; ++i) printf("%s\"%s\"", i ? "," : "", names[i]);
    printf("]%s", last ? "" : ",");
}

static int List(void) {
    const char *decoders[64], *encoders[64], *demuxers[64], *muxers[64], *protocols[64];
    int nd = 0, ne = 0, ndm = 0, nm = 0, np = 0;
    void *it = NULL;
    const AVCodec *codec;
    while ((codec = av_codec_iterate(&it)) && nd < 64 && ne < 64) {
        if (av_codec_is_decoder(codec)) decoders[nd++] = codec->name;
        if (av_codec_is_encoder(codec)) encoders[ne++] = codec->name;
    }
    it = NULL;
    const AVInputFormat *in;
    while ((in = av_demuxer_iterate(&it)) && ndm < 64) demuxers[ndm++] = in->name;
    it = NULL;
    const AVOutputFormat *out;
    while ((out = av_muxer_iterate(&it)) && nm < 64) muxers[nm++] = out->name;
    for (int output = 0; output <= 1; ++output) {
        it = NULL;
        const char *name;
        while ((name = avio_enum_protocols(&it, output)) && np < 64) protocols[np++] = name;
    }

    printf("{\"versions\":{\"avutil\":[%u,%u],\"avcodec\":[%u,%u],\"avformat\":[%u,%u],"
           "\"swscale\":[%u,%u],\"swresample\":[%u,%u]},",
           avutil_version(), (unsigned)LIBAVUTIL_VERSION_INT,
           avcodec_version(), (unsigned)LIBAVCODEC_VERSION_INT,
           avformat_version(), (unsigned)LIBAVFORMAT_VERSION_INT,
           swscale_version(), (unsigned)LIBSWSCALE_VERSION_INT,
           swresample_version(), (unsigned)LIBSWRESAMPLE_VERSION_INT);
    PrintNames("decoders", decoders, nd, 0);
    PrintNames("encoders", encoders, ne, 0);
    PrintNames("demuxers", demuxers, ndm, 0);
    PrintNames("muxers", muxers, nm, 0);
    PrintNames("protocols", protocols, np, 1);
    printf("}\n");
    return 0;
}

static int OpenDecoder(AVStream *stream, Decoder *decoder) {
    const AVCodec *codec = avcodec_find_decoder(stream->codecpar->codec_id);
    if (!codec) return AVERROR_DECODER_NOT_FOUND;
    decoder->ctx = avcodec_alloc_context3(codec);
    if (!decoder->ctx) return AVERROR(ENOMEM);
    int err = avcodec_parameters_to_context(decoder->ctx, stream->codecpar);
    if (err < 0) return err;
    decoder->ctx->thread_count = 0;
    return avcodec_open2(decoder->ctx, codec, NULL);
}

static int HandleVideo(Decoder *d, const AVFrame *frame, Stats *stats) {
    if (!d->sws) {
        d->sws = sws_getContext(frame->width, frame->height, frame->format, frame->width,
                                frame->height, AV_PIX_FMT_RGBA, SWS_BILINEAR, NULL, NULL,
                                NULL);
        d->rgba = av_malloc((size_t)frame->width * (size_t)frame->height * 4u);
        if (!d->sws || !d->rgba) return AVERROR(ENOMEM);
        stats->width = frame->width;
        stats->height = frame->height;
    }
    uint8_t *dst[4] = {d->rgba, NULL, NULL, NULL};
    int stride[4] = {frame->width * 4, 0, 0, 0};
    int rows = sws_scale(d->sws, (const uint8_t *const *)frame->data, frame->linesize, 0,
                         frame->height, dst, stride);
    if (rows != frame->height) return AVERROR_EXTERNAL;
    stats->rgba_converted++;
    stats->video_frames++;
    return 0;
}

static int HandleAudio(Decoder *d, const AVFrame *frame, Stats *stats) {
    if (!d->swr) {
        AVChannelLayout stereo = AV_CHANNEL_LAYOUT_STEREO;
        int err = swr_alloc_set_opts2(&d->swr, &stereo, AV_SAMPLE_FMT_S16,
                                      frame->sample_rate, &frame->ch_layout,
                                      frame->format, frame->sample_rate, 0, NULL);
        if (err < 0) return err;
        if ((err = swr_init(d->swr)) < 0) return err;
    }
    int capacity = swr_get_out_samples(d->swr, frame->nb_samples);
    uint8_t *pcm = av_malloc((size_t)capacity * 4u + 4u);
    if (!pcm) return AVERROR(ENOMEM);
    int converted = swr_convert(d->swr, &pcm, capacity,
                                (const uint8_t *const *)frame->extended_data,
                                frame->nb_samples);
    av_free(pcm);
    if (converted < 0) return converted;
    stats->audio_samples += converted;
    return 0;
}

/* Sends one packet (NULL flushes) and drains every frame it produces. */
static int Feed(Decoder *d, const AVPacket *packet, AVFrame *frame, Stats *stats,
                int is_video) {
    int err = avcodec_send_packet(d->ctx, packet);
    if (err < 0 && err != AVERROR_EOF) return err;
    for (;;) {
        err = avcodec_receive_frame(d->ctx, frame);
        if (err == AVERROR(EAGAIN) || err == AVERROR_EOF) return 0;
        if (err < 0) return err;
        err = is_video ? HandleVideo(d, frame, stats) : HandleAudio(d, frame, stats);
        av_frame_unref(frame);
        if (err < 0) return err;
    }
}

/* Seeks to the middle of the file and requires a decodable video frame. */
static int SeekCheck(AVFormatContext *fmt, int video_index, Decoder *video, AVPacket *packet,
                     AVFrame *frame) {
    int64_t target = fmt->duration > 0 ? fmt->duration / 2 : 0;
    int err = av_seek_frame(fmt, -1, target, AVSEEK_FLAG_BACKWARD);
    if (err < 0) return err;
    avcodec_flush_buffers(video->ctx);
    while ((err = av_read_frame(fmt, packet)) >= 0) {
        if (packet->stream_index == video_index) {
            err = avcodec_send_packet(video->ctx, packet);
            av_packet_unref(packet);
            if (err < 0) return err;
            err = avcodec_receive_frame(video->ctx, frame);
            if (err == 0) {
                av_frame_unref(frame);
                return 1;
            }
            if (err != AVERROR(EAGAIN)) return err;
        } else {
            av_packet_unref(packet);
        }
    }
    if (err != AVERROR_EOF) return err;
    /* Frame threading and B-frame reordering delay output past the last
     * packet; drain the decoder before judging the seek. */
    if ((err = avcodec_send_packet(video->ctx, NULL)) < 0) return err;
    err = avcodec_receive_frame(video->ctx, frame);
    if (err == 0) {
        av_frame_unref(frame);
        return 1;
    }
    return err == AVERROR_EOF ? 0 : err;
}

static void CloseDecoder(Decoder *d) {
    avcodec_free_context(&d->ctx);
    sws_freeContext(d->sws);
    swr_free(&d->swr);
    av_free(d->rgba);
}

static int Decode(const char *path) {
    Memory memory = {0};
    if (!LoadFile(path, &memory)) {
        fprintf(stderr, "FAIL cannot read %s\n", path);
        return 1;
    }
    int status = 1, err;
    Decoder video = {0}, audio = {0};
    Stats stats = {0};
    AVPacket *packet = av_packet_alloc();
    AVFrame *frame = av_frame_alloc();
    AVFormatContext *fmt = avformat_alloc_context();
    uint8_t *io_buffer = av_malloc(IO_BUFFER_SIZE);
    AVIOContext *io = avio_alloc_context(io_buffer, IO_BUFFER_SIZE, 0, &memory, ReadPacket,
                                         NULL, Seek);
    if (!packet || !frame || !fmt || !io) {
        fprintf(stderr, "FAIL allocation\n");
        goto done;
    }
    fmt->pb = io;
    fmt->flags |= AVFMT_FLAG_CUSTOM_IO;
    if ((err = avformat_open_input(&fmt, NULL, NULL, NULL)) < 0) {
        status = Fail("avformat_open_input", err);
        goto done;
    }
    if ((err = avformat_find_stream_info(fmt, NULL)) < 0) {
        status = Fail("avformat_find_stream_info", err);
        goto done;
    }
    int vi = av_find_best_stream(fmt, AVMEDIA_TYPE_VIDEO, -1, -1, NULL, 0);
    int ai = av_find_best_stream(fmt, AVMEDIA_TYPE_AUDIO, -1, -1, NULL, 0);
    const int indexes[2] = {vi, ai};
    for (int i = 0; i < 2; ++i) {
        if (indexes[i] < 0) continue;
        enum AVCodecID id = fmt->streams[indexes[i]]->codecpar->codec_id;
        if (!avcodec_find_decoder(id)) {
            /* The trimmed SDK must report codecs it was not built with by name. */
            printf("{\"status\":\"unsupported\",\"stream\":\"%s\",\"codec\":\"%s\"}\n",
                   i == 0 ? "video" : "audio", avcodec_get_name(id));
            status = 0;
            goto done;
        }
    }
    if (vi >= 0 && (err = OpenDecoder(fmt->streams[vi], &video)) < 0) {
        status = Fail("open video decoder", err);
        goto done;
    }
    if (ai >= 0 && (err = OpenDecoder(fmt->streams[ai], &audio)) < 0) {
        status = Fail("open audio decoder", err);
        goto done;
    }
    while ((err = av_read_frame(fmt, packet)) >= 0) {
        int result = 0;
        if (packet->stream_index == vi) result = Feed(&video, packet, frame, &stats, 1);
        else if (packet->stream_index == ai) result = Feed(&audio, packet, frame, &stats, 0);
        av_packet_unref(packet);
        if (result < 0) {
            status = Fail("decode", result);
            goto done;
        }
    }
    if (err != AVERROR_EOF) {
        status = Fail("av_read_frame", err);
        goto done;
    }
    if ((vi >= 0 && (err = Feed(&video, NULL, frame, &stats, 1)) < 0) ||
        (ai >= 0 && (err = Feed(&audio, NULL, frame, &stats, 0)) < 0)) {
        status = Fail("flush", err);
        goto done;
    }
    int seek_ok = 0;
    if (vi >= 0) {
        seek_ok = SeekCheck(fmt, vi, &video, packet, frame);
        if (seek_ok < 0) {
            status = Fail("seek", seek_ok);
            goto done;
        }
    }

    printf("{\"status\":\"ok\"");
    if (vi >= 0)
        printf(",\"video_codec\":\"%s\",\"width\":%d,\"height\":%d,\"video_frames\":%d,"
               "\"rgba_frames\":%d,\"seek\":%s",
               avcodec_get_name(video.ctx->codec_id), stats.width, stats.height,
               stats.video_frames, stats.rgba_converted, seek_ok ? "true" : "false");
    if (ai >= 0)
        printf(",\"audio_codec\":\"%s\",\"sample_rate\":%d,\"channels\":%d,"
               "\"audio_samples\":%lld",
               avcodec_get_name(audio.ctx->codec_id), audio.ctx->sample_rate,
               audio.ctx->ch_layout.nb_channels, (long long)stats.audio_samples);
    printf("}\n");
    status = 0;

done:
    CloseDecoder(&video);
    CloseDecoder(&audio);
    if (fmt && (fmt->iformat || fmt->nb_streams)) avformat_close_input(&fmt);
    else avformat_free_context(fmt);
    if (io) {
        av_freep(&io->buffer);
        avio_context_free(&io);
    } else {
        av_free(io_buffer);
    }
    av_frame_free(&frame);
    av_packet_free(&packet);
    free(memory.data);
    return status;
}

int main(int argc, char **argv) {
    if (argc == 2 && strcmp(argv[1], "--list") == 0) return List();
    if (argc == 2) return Decode(argv[1]);
    fprintf(stderr, "usage: %s --list | <media-file>\n", argv[0]);
    return 2;
}
