/**
 * pcm-worklet.js
 * AudioWorkletProcessor that downsamples mic audio to 16 kHz mono Int16 PCM
 * and posts ArrayBuffers to the main thread for WebSocket transmission.
 *
 * The AudioContext sample rate (available as global `sampleRate`) is typically
 * 44100 or 48000 Hz. We downsample to 16000 Hz by accumulating samples and
 * stepping through them with a fixed ratio.
 */

const TARGET_SAMPLE_RATE = 16000;

class PCMProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super(options);
    // Accumulation buffer for input samples before downsampling
    this._accumulator = [];
    // Fractional position in the input for the downsampling cursor
    this._phase = 0;
    // Ratio: how many input samples per one output sample
    this._ratio = sampleRate / TARGET_SAMPLE_RATE;
  }

  process(inputs) {
    const input = inputs[0];
    if (!input || !input[0] || input[0].length === 0) {
      return true;
    }

    const channelData = input[0]; // Float32Array, mono

    // Downsample via nearest-neighbour stepping.
    // For each output sample we need, advance the phase by ratio through the input.
    const outputSamples = [];
    for (let i = 0; i < channelData.length; i++) {
      this._accumulator.push(channelData[i]);
    }

    // Process accumulated samples
    while (this._phase < this._accumulator.length) {
      const idx = Math.floor(this._phase);
      // Linear interpolation between adjacent samples for slightly better quality
      const next = Math.min(idx + 1, this._accumulator.length - 1);
      const frac = this._phase - idx;
      const sample = this._accumulator[idx] * (1 - frac) + this._accumulator[next] * frac;
      // Clamp and convert Float32 [-1, 1] to Int16 [-32767, 32767]
      const clamped = Math.max(-1, Math.min(1, sample));
      outputSamples.push(Math.round(clamped * 32767));
      this._phase += this._ratio;
    }

    // Discard consumed input, keep the fractional remainder
    const consumed = Math.floor(this._phase);
    this._accumulator = this._accumulator.slice(consumed);
    this._phase -= consumed;

    if (outputSamples.length === 0) {
      return true;
    }

    // Pack output samples into an Int16 little-endian ArrayBuffer and transfer it
    const int16Buffer = new Int16Array(outputSamples);
    this.port.postMessage(int16Buffer.buffer, [int16Buffer.buffer]);

    return true;
  }
}

registerProcessor('pcm-processor', PCMProcessor);
