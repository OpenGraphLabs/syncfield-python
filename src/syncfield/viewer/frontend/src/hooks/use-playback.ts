import { useCallback, useEffect, useRef, useState } from "react";

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

// Throttle React state updates to ~15 Hz for smooth UI without
// causing 60 re-renders/sec that would stutter secondary video sync.
const STATE_UPDATE_INTERVAL_MS = 66;

/**
 * Central playback state for the review video player.
 *
 * The primary `<video>` element handles its own decoding and rendering
 * natively (no React interference). This hook only syncs the timeline
 * display at ~15 Hz to avoid excessive re-renders.
 */
export function usePlayback(): UsePlaybackReturn {
  const [currentTime, setCurrentTime] = useState(0);
  const [duration, setDuration] = useState(0);
  const [isPlaying, setIsPlaying] = useState(false);
  const [playbackRate, setPlaybackRateState] = useState(1);

  const videoElRef = useRef<HTMLVideoElement | null>(null);
  const rafRef = useRef<number | null>(null);
  const lastUpdateRef = useRef(0);

  const startTimeSync = useCallback(() => {
    const tick = (now: number) => {
      const el = videoElRef.current;
      if (!el) return;

      // Throttle React state updates
      if (now - lastUpdateRef.current >= STATE_UPDATE_INTERVAL_MS) {
        setCurrentTime(el.currentTime);
        lastUpdateRef.current = now;
      }

      rafRef.current = requestAnimationFrame(tick);
    };
    rafRef.current = requestAnimationFrame(tick);
  }, []);

  const stopTimeSync = useCallback(() => {
    if (rafRef.current !== null) {
      cancelAnimationFrame(rafRef.current);
      rafRef.current = null;
    }
    // Final sync on pause
    const el = videoElRef.current;
    if (el) setCurrentTime(el.currentTime);
  }, []);

  const videoRef = useCallback(
    (el: HTMLVideoElement | null) => {
      const prev = videoElRef.current;
      if (prev) {
        prev.onplay = null;
        prev.onpause = null;
        prev.onended = null;
        prev.onloadedmetadata = null;
        prev.ondurationchange = null;
        prev.onseeked = null;
        stopTimeSync();
      }

      videoElRef.current = el;

      if (el) {
        el.onplay = () => {
          setIsPlaying(true);
          startTimeSync();
        };
        el.onpause = () => {
          setIsPlaying(false);
          stopTimeSync();
        };
        el.onended = () => {
          setIsPlaying(false);
          stopTimeSync();
        };
        el.onloadedmetadata = () => setDuration(el.duration);
        el.ondurationchange = () => setDuration(el.duration);
        el.onseeked = () => setCurrentTime(el.currentTime);

        if (el.duration) setDuration(el.duration);
        setCurrentTime(el.currentTime);
        if (!el.paused) {
          setIsPlaying(true);
          startTimeSync();
        }
      }
    },
    [startTimeSync, stopTimeSync],
  );

  useEffect(() => {
    return () => {
      if (rafRef.current !== null) cancelAnimationFrame(rafRef.current);
    };
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
