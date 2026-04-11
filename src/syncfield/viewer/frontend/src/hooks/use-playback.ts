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

/**
 * Central playback state for the review video player.
 *
 * Uses requestAnimationFrame during playback for smooth timeline
 * updates (~60 Hz), falling back to event handlers for state changes.
 */
export function usePlayback(): UsePlaybackReturn {
  const [currentTime, setCurrentTime] = useState(0);
  const [duration, setDuration] = useState(0);
  const [isPlaying, setIsPlaying] = useState(false);
  const [playbackRate, setPlaybackRateState] = useState(1);

  const videoElRef = useRef<HTMLVideoElement | null>(null);
  const rafRef = useRef<number | null>(null);
  const playingRef = useRef(false);

  // Smooth time sync loop during playback
  const startTimeSync = useCallback(() => {
    const tick = () => {
      const el = videoElRef.current;
      if (el && playingRef.current) {
        setCurrentTime(el.currentTime);
        rafRef.current = requestAnimationFrame(tick);
      }
    };
    rafRef.current = requestAnimationFrame(tick);
  }, []);

  const stopTimeSync = useCallback(() => {
    if (rafRef.current !== null) {
      cancelAnimationFrame(rafRef.current);
      rafRef.current = null;
    }
    // One final sync
    const el = videoElRef.current;
    if (el) setCurrentTime(el.currentTime);
  }, []);

  const videoRef = useCallback((el: HTMLVideoElement | null) => {
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
        playingRef.current = true;
        setIsPlaying(true);
        startTimeSync();
      };
      el.onpause = () => {
        playingRef.current = false;
        setIsPlaying(false);
        stopTimeSync();
      };
      el.onended = () => {
        playingRef.current = false;
        setIsPlaying(false);
        stopTimeSync();
      };
      el.onloadedmetadata = () => setDuration(el.duration);
      el.ondurationchange = () => setDuration(el.duration);
      el.onseeked = () => setCurrentTime(el.currentTime);

      if (el.duration) setDuration(el.duration);
      setCurrentTime(el.currentTime);
      const wasPlaying = !el.paused;
      playingRef.current = wasPlaying;
      setIsPlaying(wasPlaying);
      if (wasPlaying) startTimeSync();
    }
  }, [startTimeSync, stopTimeSync]);

  // Cleanup on unmount
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
