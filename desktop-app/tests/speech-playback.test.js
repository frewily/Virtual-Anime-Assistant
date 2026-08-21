const assert = require('node:assert/strict');
const test = require('node:test');

const {
    MAX_PROCESSED_CORRELATIONS,
    createSpeechPlayback
} = require('../src/renderer/js/speech-playback');

function audioHarness({ rejectPlay = false } = {}) {
    const urls = [];
    class FakeAudio {
        constructor(url) {
            urls.push(url);
        }

        play() {
            return rejectPlay
                ? Promise.reject(new Error('blocked'))
                : Promise.resolve();
        }
    }
    return { AudioCtor: FakeAudio, urls };
}

test('plays an existing backend audio URL without requesting TTS', async () => {
    const audio = audioHarness();
    let fetchCalls = 0;
    const playback = createSpeechPlayback({
        AudioCtor: audio.AudioCtor,
        fetchImpl: async () => {
            fetchCalls += 1;
            throw new Error('unexpected fetch');
        }
    });

    assert.equal(await playback.handleSpeakAudio({
        correlationId: 'existing-audio',
        audioUrl: '/api/tts/audio/example.mp3',
        text: '不会重复合成'
    }), true);
    assert.equal(fetchCalls, 0);
    assert.deepEqual(audio.urls, [
        'http://127.0.0.1:8080/api/tts/audio/example.mp3'
    ]);
});

test('requests TTS once for text-only speak and plays the result', async () => {
    const audio = audioHarness();
    const requests = [];
    const playback = createSpeechPlayback({
        AudioCtor: audio.AudioCtor,
        fetchImpl: async (url, options) => {
            requests.push({ url, options });
            return {
                ok: true,
                async json() {
                    return { audio_url: '/api/tts/audio/generated.mp3' };
                }
            };
        }
    });

    assert.equal(await playback.handleSpeakAudio({
        correlationId: 'generated-audio',
        text: '  主人你好  '
    }), true);
    assert.equal(requests.length, 1);
    assert.equal(requests[0].url, 'http://127.0.0.1:8080/api/tts/speak');
    assert.equal(requests[0].options.method, 'POST');
    assert.equal(requests[0].options.headers['Content-Type'], 'application/json');
    assert.deepEqual(JSON.parse(requests[0].options.body), {
        text: '主人你好'
    });
    assert.deepEqual(audio.urls, [
        'http://127.0.0.1:8080/api/tts/audio/generated.mp3'
    ]);
});

test('packaged playback fetches audio with the ephemeral token before playing', async () => {
    const audio = audioHarness();
    const requests = [];
    const revoked = [];
    const playback = createSpeechPlayback({
        AudioCtor: audio.AudioCtor,
        accessToken: 'a'.repeat(43),
        fetchImpl: async (url, options) => {
            requests.push({ url, options });
            return {
                ok: true,
                async blob() { return { type: 'audio/mpeg' }; }
            };
        },
        urlApi: {
            createObjectURL: () => 'blob:authorized-audio',
            revokeObjectURL: (url) => revoked.push(url)
        }
    });

    assert.equal(await playback.handleSpeakAudio({
        correlationId: 'protected-audio',
        audioUrl: '/api/tts/audio/protected.mp3'
    }), true);
    assert.equal(requests.length, 1);
    assert.equal(requests[0].options.headers['X-VAA-Desktop-Token'], 'a'.repeat(43));
    assert.deepEqual(audio.urls, ['blob:authorized-audio']);
    assert.deepEqual(revoked, []);
});

test('ignores blank text and rejects cross-origin audio URLs', async () => {
    const audio = audioHarness();
    let fetchCalls = 0;
    const playback = createSpeechPlayback({
        AudioCtor: audio.AudioCtor,
        fetchImpl: async () => {
            fetchCalls += 1;
            throw new Error('unexpected fetch');
        }
    });

    assert.equal(await playback.handleSpeakAudio({ text: '   ' }), false);
    assert.equal(await playback.handleSpeakAudio({
        correlationId: 'cross-origin',
        audioUrl: 'https://example.test/audio.mp3'
    }), false);
    assert.equal(fetchCalls, 0);
    assert.deepEqual(audio.urls, []);
});

test('contains request, response, and playback failures', async () => {
    const warnings = [];
    const requestFailure = createSpeechPlayback({
        AudioCtor: audioHarness().AudioCtor,
        fetchImpl: async () => ({ ok: false }),
        logger: { warn: (message) => warnings.push(message) }
    });
    assert.equal(await requestFailure.handleSpeakAudio({
        correlationId: 'request-failure',
        text: '测试'
    }), false);

    const malformedResponse = createSpeechPlayback({
        AudioCtor: audioHarness().AudioCtor,
        fetchImpl: async () => ({
            ok: true,
            async json() {
                return {};
            }
        }),
        logger: { warn: (message) => warnings.push(message) }
    });
    assert.equal(await malformedResponse.handleSpeakAudio({
        correlationId: 'response-failure',
        text: '测试'
    }), false);

    const rejectedAudio = audioHarness({ rejectPlay: true });
    const playbackFailure = createSpeechPlayback({
        AudioCtor: rejectedAudio.AudioCtor,
        fetchImpl: async () => ({
            ok: true,
            async json() {
                return { audio_url: '/api/tts/audio/rejected.mp3' };
            }
        }),
        logger: { warn: (message) => warnings.push(message) }
    });
    assert.equal(await playbackFailure.handleSpeakAudio({
        correlationId: 'playback-failure',
        text: '测试'
    }), false);
    assert.deepEqual(warnings, [
        'Live2D TTS request failed',
        'Live2D TTS request failed',
        'Live2D audio playback failed'
    ]);
});

test('deduplicates correlations and bounds the processed cache', async () => {
    const audio = audioHarness();
    const playback = createSpeechPlayback({
        AudioCtor: audio.AudioCtor,
        fetchImpl: async () => {
            throw new Error('unexpected fetch');
        }
    });

    for (
        let index = 1;
        index <= MAX_PROCESSED_CORRELATIONS + 1;
        index += 1
    ) {
        assert.equal(await playback.handleSpeakAudio({
            correlationId: `message-${index}`,
            audioUrl: '/api/tts/audio/example.mp3'
        }), true);
    }
    assert.equal(playback.processedCount(), MAX_PROCESSED_CORRELATIONS);
    assert.equal(await playback.handleSpeakAudio({
        correlationId: `message-${MAX_PROCESSED_CORRELATIONS + 1}`,
        audioUrl: '/api/tts/audio/example.mp3'
    }), false);
    assert.equal(await playback.handleSpeakAudio({
        correlationId: 'message-1',
        audioUrl: '/api/tts/audio/example.mp3'
    }), true);
});
