// Windowing for long lists with rows of one height: only the rows in view
// (plus a margin) are in the DOM, however many there are. The scroll
// container is the caller's element; padding above and below keeps its
// scroll height what it would be with every row rendered.

import { useCallback, useLayoutEffect, useRef, useState } from "react";

export interface VirtualWindow<E extends HTMLElement> {
  containerRef: React.RefObject<E | null>;
  onScroll: () => void;
  start: number;
  end: number;
  padTop: number;
  padBottom: number;
  scrollToIndex: (index: number) => void;
}

export function useVirtual<E extends HTMLElement = HTMLDivElement>(
  count: number,
  rowHeight: number,
  overscan = 10,
  headerHeight = 0,
): VirtualWindow<E> {
  const containerRef = useRef<E | null>(null);
  const [scrollTop, setScrollTop] = useState(0);
  const [height, setHeight] = useState(600);

  useLayoutEffect(() => {
    const element = containerRef.current;
    if (!element) return;
    const measure = () => setHeight(element.clientHeight || 600);
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  const onScroll = useCallback(() => {
    const element = containerRef.current;
    if (element) setScrollTop(element.scrollTop);
  }, []);

  const first = Math.max(0, Math.floor((scrollTop - headerHeight) / rowHeight) - overscan);
  const visible = Math.ceil(height / rowHeight) + 2 * overscan;
  const start = Math.min(first, Math.max(0, count - 1));
  const end = Math.min(count, start + visible);

  const scrollToIndex = useCallback(
    (index: number) => {
      const element = containerRef.current;
      if (!element) return;
      const top = headerHeight + index * rowHeight;
      const viewTop = element.scrollTop + headerHeight;
      const viewBottom = element.scrollTop + element.clientHeight;
      if (top < viewTop) element.scrollTop = top - headerHeight;
      else if (top + rowHeight > viewBottom) element.scrollTop = top + rowHeight - element.clientHeight;
    },
    [rowHeight, headerHeight],
  );

  return {
    containerRef,
    onScroll,
    start: count === 0 ? 0 : start,
    end,
    padTop: (count === 0 ? 0 : start) * rowHeight,
    padBottom: Math.max(0, count - end) * rowHeight,
    scrollToIndex,
  };
}
