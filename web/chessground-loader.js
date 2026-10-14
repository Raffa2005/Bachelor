import { Chessground } from "/vendor/chessground/10.1.1/dist/chessground.min.js";

window.Chessground = Chessground;
window.dispatchEvent(new Event("chessground-ready"));
