const DEFAULT_BACKEND_URL = 'http://127.0.0.1:8080';
const MAX_PROCESSED_CORRELATIONS = 100;

function createSpeechPlayback({
    fetchImpl = (...args) => window.fetch(...args),
    AudioCtor = window.Audio,
    logger = console,
    backendUrl = DEFAULT_BACKEND_URL,
    accessToken = null,
    urlApi = globalThis.URL
} = {}) {
    const backend = new URL(backendUrl);
    const processed = new Set();

    function remember(correlationId) {
        if (typeof correlationId !== 'string' || !correlationId.trim()) {
            return true;
        }
        const normalized = correlationId.trim();
        if (processed.has(normalized)) return false;
        processed.add(normalized);
        while (processed.size > MAX_PROCESSED_CORRELATIONS) {
            processed.delete(processed.values().next().value);
        }
        return true;
    }

    function resolveAudioUrl(value) {
        if (typeof value !== 'string' || !value.trim()) return null;
        try {
            const resolved = new URL(value.trim(), backend);
            return resolved.origin === backend.origin
                ? resolved.toString()
                : null;
        } catch {
            return null;
        }
    }

    function accessHeaders(headers = {}) {
        return accessToken
            ? { ...headers, 'X-VAA-Desktop-Token': accessToken }
            : { ...headers };
    }

    async function authorizedAudioUrl(url) {
        if (!accessToken) return { url, revoke: null };
        const response = await fetchImpl(url, {
            headers: accessHeaders({ Accept: 'audio/*' })
        });
        if (!response.ok) throw new Error('audio request failed');
        const objectUrl = urlApi.createObjectURL(await response.blob());
        return {
            url: objectUrl,
            revoke: () => urlApi.revokeObjectURL(objectUrl)
        };
    }

    async function playAudio(value) {
        const url = resolveAudioUrl(value);
        if (!url) return false;
        let playable = null;
        try {
            playable = await authorizedAudioUrl(url);
            const audio = new AudioCtor(playable.url);
            if (playable.revoke && typeof audio.addEventListener === 'function') {
                audio.addEventListener('ended', playable.revoke, { once: true });
                audio.addEventListener('error', playable.revoke, { once: true });
            }
            await Promise.resolve(audio.play());
            return true;
        } catch {
            if (playable?.revoke) playable.revoke();
            logger.warn('Live2D audio playback failed');
            return false;
        }
    }

    async function handleSpeakAudio(message) {
        const audioUrl = resolveAudioUrl(message?.audioUrl);
        const text = typeof message?.text === 'string'
            ? message.text.trim()
            : '';
        if (!audioUrl && !text) return false;
        if (!remember(message?.correlationId)) return false;
        if (audioUrl) return playAudio(audioUrl);

        try {
            const response = await fetchImpl(
                new URL('/api/tts/speak', backend).toString(),
                {
                    method: 'POST',
                    headers: accessHeaders({ 'Content-Type': 'application/json' }),
                    body: JSON.stringify({ text })
                }
            );
            if (!response.ok) throw new Error('tts request failed');
            const payload = await response.json();
            const generated = resolveAudioUrl(payload?.audio_url);
            if (!generated) throw new Error('invalid tts response');
            return playAudio(generated);
        } catch {
            logger.warn('Live2D TTS request failed');
            return false;
        }
    }

    return {
        handleSpeakAudio,
        processedCount: () => processed.size
    };
}

module.exports = {
    DEFAULT_BACKEND_URL,
    MAX_PROCESSED_CORRELATIONS,
    createSpeechPlayback
};
