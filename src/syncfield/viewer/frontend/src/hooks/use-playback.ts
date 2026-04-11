import { useCallback, useRef, useState } from "react";

interface UsePlaybackReturn {
  currentTime: number;
  duration: number;
  isPlaying: boolean;
  playbackRate: number;
  play: () => void;
  pause: () => void;
  toggle: () => void;
  seek: (time: number) => void;
  setPlaybackRate: (rate: number) => void;
  videoRef: (el: HTMLVideoElement | null) => void;
}

/**
 * Central playback state for the review video player.
 *
 * Uses `timeupdate` events from the video element (fires ~4 Hz during
 * playback) to sync React state. This is more reliable than rAF for
 * <video> elements since it only fires when the video actually advances.
 */
export function usePlayback(): UsePlaybackReturn {
  const [currentTime, setCurrentTime] = useState(0);
  const [duration, setDuration] = useState(0);
  const [isPlaying, setIsPlaying] = useState(false);
  const [playbackRate, setPlaybackRateState] = useState(1);

  const videoElRef = useRef<HTMLVideoElement | null>(null);

  const videoRef = useCallback((el: HTMLVideoElement | null) => {
    const prev = videoElRef.current;

    // Detach from previous element
    if (prev) {
      prev.ontimeupdate = null;
      prev.onplay = null;
      prev.onpause = null;
      prev.onended = null;
      prev.onloadedmetadata = null;
      prev.ondurationchange = null;
    }

    videoElRef.current = el;

    if (el) {
      el.ontimeupdate = () => setCurrentTime(el.currentTime);
      el.onplay = () => setIsPlaying(true);
      el.onpause = () => setIsPlaying(false);
      el.onended = () => setIsPlaying(false);
      el.onloadedmetadata = () => setDuration(el.duration);
      el.ondurationchange = () => setDuration(el.duration);

      // Sync initial state
      if (el.duration) setDuration(el.duration);
      setCurrentTime(el.currentTime);
      setIsPlaying(!el.paused);
    }
  }, []);

  const play = useCallback(() => {
    void videoElRef.current?.play();
  }, []);

  const pause = useCallback(() => {
    videoElRef.current?.pause();
  }, []);

  const toggle = useCallback(() => {
    const el = videoElRef.current;
    if (!el) return;
    if (el.paused) void el.play();
    else el.pause();
  }, []);

  const seek = useCallback((time: number) => {
    const el = videoElRef.current;
    if (!el) return;
    el.currentTime = time;
    setCurrentTime(time);
  }, []);

  const setPlaybackRate = useCallback((rate: number) => {
    setPlaybackRateState(rate);
    const el = videoElRef.current;
    if (el) el.playbackRate = rate;
  }, []);

  return {
    currentTime,
    duration,
    isPlaying,
    playbackRate,
    play,
    pause,
    toggle,
    seek,
    setPlaybackRate,
    videoRef,
  };
}
