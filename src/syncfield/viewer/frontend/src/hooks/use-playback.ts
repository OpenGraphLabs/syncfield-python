import { useCallback, useEffect, useRef, useState } from "react";

interface UsePlaybackReturn {
  /** Current playback time in seconds. */
  currentTime: number;
  /** Total duration of the primary video in seconds. */
  duration: number;
  /** Whether the video is currently playing. */
  isPlaying: boolean;
  /** Current playback rate (e.g. 1.0, 0.5, 2.0). */
  playbackRate: number;
  /** Start playback. */
  play: () => void;
  /** Pause playback. */
  pause: () => void;
  /** Toggle play/pause. */
  toggle: () => void;
  /** Seek to a specific time in seconds. */
  seek: (time: number) => void;
  /** Set the playback rate. */
  setPlaybackRate: (rate: number) => void;
  /** Ref to attach to the primary <video> element. */
  videoRef: React.RefCallback<HTMLVideoElement>;
}

/**
 * Central playback state for the review video player.
 *
 * Manages a ref to the primary video element and exposes play, pause,
 * seek, and playback rate controls. The `videoRef` callback ref should
 * be attached to the primary `<video>` element. Secondary camera videos
 * can sync to `currentTime` via their own `useEffect`.
 */
export function usePlayback(): UsePlaybackReturn {
  const [currentTime, setCurrentTime] = useState(0);
  const [duration, setDuration] = useState(0);
  const [isPlaying, setIsPlaying] = useState(false);
  const [playbackRate, setPlaybackRateState] = useState(1);

  const videoElRef = useRef<HTMLVideoElement | null>(null);
  const rafRef = useRef<number | null>(null);

  // Tick loop to sync currentTime from the video element to React state
  const tick = useCallback(() => {
    const el = videoElRef.current;
    if (el) {
      setCurrentTime(el.currentTime);
    }
    rafRef.current = requestAnimationFrame(tick);
  }, []);

  // Start/stop the tick loop based on playing state
  useEffect(() => {
    if (isPlaying) {
      rafRef.current = requestAnimationFrame(tick);
    } else {
      if (rafRef.current !== null) {
        cancelAnimationFrame(rafRef.current);
        rafRef.current = null;
      }
      // Sync one last time when paused
      const el = videoElRef.current;
      if (el) setCurrentTime(el.currentTime);
    }
    return () => {
      if (rafRef.current !== null) {
        cancelAnimationFrame(rafRef.current);
        rafRef.current = null;
      }
    };
  }, [isPlaying, tick]);

  // Callback ref for the primary video element
  const videoRef = useCallback(
    (el: HTMLVideoElement | null) => {
      const prev = videoElRef.current;

      // Remove listeners from previous element
      if (prev) {
        prev.removeEventListener("play", handlePlay);
        prev.removeEventListener("pause", handlePause);
        prev.removeEventListener("ended", handlePause);
        prev.removeEventListener("loadedmetadata", handleMetadata);
      }

      videoElRef.current = el;

      // Attach listeners to new element
      if (el) {
        el.addEventListener("play", handlePlay);
        el.addEventListener("pause", handlePause);
        el.addEventListener("ended", handlePause);
        el.addEventListener("loadedmetadata", handleMetadata);
        el.playbackRate = playbackRate;
        setDuration(el.duration || 0);
        setCurrentTime(el.currentTime);
        setIsPlaying(!el.paused);
      }
    },
    // playbackRate is needed so new elements get the current rate
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [playbackRate],
  );

  function handlePlay() {
    setIsPlaying(true);
  }

  function handlePause() {
    setIsPlaying(false);
  }

  function handleMetadata() {
    const el = videoElRef.current;
    if (el) setDuration(el.duration);
  }

  const play = useCallback(() => {
    void videoElRef.current?.play();
  }, []);

  const pause = useCallback(() => {
    videoElRef.current?.pause();
  }, []);

  const toggle = useCallback(() => {
    const el = videoElRef.current;
    if (!el) return;
    if (el.paused) {
      void el.play();
    } else {
      el.pause();
    }
  }, []);

  const seek = useCallback((time: number) => {
    const el = videoElRef.current;
    if (el) {
      el.currentTime = time;
      setCurrentTime(time);
    }
  }, []);

  const setPlaybackRate = useCallback((rate: number) => {
    setPlaybackRateState(rate);
    const el = videoElRef.current;
    if (el) {
      el.playbackRate = rate;
    }
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
