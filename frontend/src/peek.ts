import { createContext, useContext } from "react";

export type Peek = { kind: "email" | "page" | "thread"; id: number | string } | null;
export const PeekCtx = createContext<{ peek: (p: Peek) => void }>({ peek: () => {} });
export const usePeek = () => useContext(PeekCtx).peek;
