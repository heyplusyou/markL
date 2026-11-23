import { Facet, StateEffect, StateField } from "@codemirror/state";
import { convertFileSrc, invoke } from "@tauri-apps/api/core";
import { EditorView, WidgetType } from "@codemirror/view";

// A fresh context per document, fetched only if that document displays an image.
// Rust returns an epoch, never a filesystem path. Save As invalidates this cache.
type ImageContext = { epoch?: Promise<number> };
const initialContext = Facet.define<ImageContext, ImageContext>({ combine: (values) => values[0] });
const resetImages = StateEffect.define<null>();
const imageContext = StateField.define<ImageContext>({
  create: (state) => state.facet(initialContext),
  update: (value, tr) => tr.effects.some((effect) => effect.is(resetImages)) ? {} : value,
});
export const imageSupport = () => [initialContext.of({}), imageContext];

export function refreshImages(view: EditorView) {
  view.dispatch({ effects: resetImages.of(null) });
}

export class MarkdownImage extends WidgetType {
  constructor(readonly source: string, readonly alt: string, readonly context: ImageContext) {
    super();
  }

  eq(other: MarkdownImage) {
    return this.source === other.source && this.alt === other.alt && this.context === other.context;
  }

  toDOM(view: EditorView) {
    const wrapper = document.createElement("span");
    wrapper.className = "cm-md-image";
    const status = document.createElement("span");
    status.className = "cm-md-image-status";
    status.textContent = this.alt ? `${this.alt} — loading image…` : "Loading image…";
    wrapper.append(status);
    const fail = () => {
      status.hidden = false;
      status.textContent = `${this.alt || "Image"} — unavailable (local PNG, JPEG, GIF, WebP or AVIF only)`;
      wrapper.querySelector("img")?.remove();
      view.requestMeasure();
    };
    // Don't even ask Rust about remote URLs, absolute paths, or data URLs.
    if (/^(?:[/\\]|[a-z][a-z\d+.-]*:)/i.test(this.source)) {
      fail();
      return wrapper;
    }
    this.context.epoch ??= invoke<number>("image_context");
    this.context.epoch.then((epoch) => {
      if (view.state.field(imageContext) !== this.context || !wrapper.isConnected) return;
      const image = document.createElement("img");
      image.alt = this.alt;
      image.loading = "lazy";
      image.decoding = "async";
      image.draggable = false;
      image.addEventListener("load", () => { status.hidden = true; view.requestMeasure(); });
      image.addEventListener("error", fail, { once: true });
      image.src = convertFileSrc(`${epoch}/${this.source}`, "markl-image");
      wrapper.prepend(image);
    }, fail);
    return wrapper;
  }

  ignoreEvent() {
    return false; // Clicking the image reveals the source on that line.
  }
}

export function imageDecoration(view: EditorView, source: string, alt: string) {
  return new MarkdownImage(source.replace(/^<|>$/g, "").replace(/\\([\\()[\]<> ])/g, "$1"), alt, view.state.field(imageContext));
}

export function imagesChanged(before: EditorView["state"], after: EditorView["state"]) {
  return before.field(imageContext) !== after.field(imageContext);
}
