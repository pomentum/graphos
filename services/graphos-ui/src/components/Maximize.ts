import { createContext, useContext } from "react";

/**
 * A view maximized to the whole window: the toolbar and the sidebar give way
 * until it is restored, or another view is opened. The window (AppLayout)
 * holds the state; a view offers the button.
 */
export interface Maximize {
  maximized: boolean;
  toggle: () => void;
  restore: () => void;
}

export const MaximizeContext = createContext<Maximize>({
  maximized: false,
  toggle: () => undefined,
  restore: () => undefined,
});

export const useMaximize = () => useContext(MaximizeContext);
