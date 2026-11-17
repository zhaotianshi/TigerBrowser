export interface FingerprintData {
  navigator: {
    userAgent: string;
    platform: string;
    oscpu: string;
    hardwareConcurrency: number;
    maxTouchPoints: number;
    vendor: string;
    buildID: string;
    doNotTrack: string;
  };
  screen: {
    width: number;
    height: number;
    colorDepth: number;
    devicePixelRatio: number;
    availWidth: number;
    availHeight: number;
    pixelDepth: number;
    innerWidth: number;
    innerHeight: number;
    outerWidth: number;
    outerHeight: number;
  };
  timezone: {
    timezone: string;
    offset: number;
    localTime: string;
  };
  webgl: {
    vendor: string;
    renderer: string;
    unmaskedVendor: string;
    unmaskedRenderer: string;
    maxTextureSize: number;
  } | null;
  canvas: { hash: string; dataUrlPrefix: string };
  audio: {
    hash: string;
    sampleRate: number;
    methods: {
      getChannelData: string;
      copyFromChannel: string;
      analyserFloat: string;
      analyserByte: string;
      analyserTimeDomainFloat: string;
      analyserTimeDomainByte: string;
    };
  };
  fonts: { measureWidth: number; hash: string };
  clientRects: { hash: string };
  emojiCanvas: { hash: string };
  fontAvailability: { detected: string[]; count: number; hash: string };
  speechVoices: { names: string[]; count: number; hash: string };
}

export interface WebRTCResult {
  passed: boolean;
  iceIPs: string[];
  sdpSanitized: boolean;
  getStatsClean: boolean;
  candidateCount: number;
  detail: string;
}
