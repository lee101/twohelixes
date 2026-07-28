/**
 * Dictation, where the browser already has it.
 *
 * The Web Speech API is the only speech recogniser that costs us nothing and
 * sends nothing of ours anywhere: it is the browser's, and on Chrome and Safari
 * it is already there. There is no server fallback on purpose - shipping audio
 * to a transcription service would make "ask for a chart out loud" a data
 * question, and the answer to a data question should not be "we uploaded your
 * microphone". Where it is missing, the button is not drawn and the text box
 * is the whole feature.
 */

import { el } from "./chart";

type Recogniser = {
  lang: string;
  continuous: boolean;
  interimResults: boolean;
  start(): void;
  stop(): void;
  onresult: ((event: any) => void) | null;
  onerror: ((event: any) => void) | null;
  onend: (() => void) | null;
};

function recogniserClass(): (new () => Recogniser) | null {
  const w = window as unknown as Record<string, any>;
  return w.SpeechRecognition ?? w.webkitSpeechRecognition ?? null;
}

export function voiceSupported(): boolean {
  return recogniserClass() !== null;
}

export interface VoiceOptions {
  /** Called with the text so far, interim included, so the box fills live. */
  onText: (text: string, final: boolean) => void;
  /** Called when a final phrase lands and the user has stopped talking. */
  onDone?: (text: string) => void;
}

/**
 * A mic toggle. Returns null when the browser has no recogniser, so callers
 * can append the result unconditionally without drawing a button that lies.
 */
export function micButton(options: VoiceOptions): HTMLButtonElement | null {
  const Recogniser = recogniserClass();
  if (!Recogniser) return null;

  const node = document.createElement("button");
  node.type = "button";
  node.className = "btn btn-ghost mic-btn";
  node.setAttribute("aria-label", "Dictate");
  node.title = "Dictate (uses this browser's speech recognition)";
  node.innerHTML =
    '<svg viewBox="0 0 24 24" aria-hidden="true" class="mic-icon">' +
    '<rect x="9" y="3" width="6" height="11" rx="3"/>' +
    '<path d="M5 11a7 7 0 0 0 14 0M12 18v3"/></svg>';

  let live: Recogniser | null = null;
  let final = "";

  const stop = () => {
    live?.stop();
    live = null;
    node.classList.remove("is-listening");
    node.setAttribute("aria-pressed", "false");
  };

  node.setAttribute("aria-pressed", "false");
  node.addEventListener("click", () => {
    if (live) {
      stop();
      return;
    }
    const recogniser = new Recogniser();
    live = recogniser;
    final = "";
    recogniser.lang = navigator.language || "en-US";
    // Continuous, because a question with a clause in it has a pause in it and
    // a single-shot recogniser cuts off at the first one.
    recogniser.continuous = true;
    recogniser.interimResults = true;

    recogniser.onresult = (event: any) => {
      let interim = "";
      for (let i = event.resultIndex; i < event.results.length; i += 1) {
        const chunk = event.results[i];
        if (chunk.isFinal) final += chunk[0].transcript;
        else interim += chunk[0].transcript;
      }
      options.onText((final + interim).trim(), false);
    };
    recogniser.onerror = () => stop();
    recogniser.onend = () => {
      const text = final.trim();
      stop();
      if (text) {
        options.onText(text, true);
        options.onDone?.(text);
      }
    };

    try {
      recogniser.start();
      node.classList.add("is-listening");
      node.setAttribute("aria-pressed", "true");
    } catch {
      // Already running, or the permission prompt was dismissed.
      stop();
    }
  });

  return node;
}

/** A text field with a mic beside it, since that pair is wanted three times. */
export function voiceField(
  placeholder: string,
  onSubmit: (text: string) => void,
): { form: HTMLFormElement; input: HTMLInputElement } {
  const form = el("form", "voice-field") as HTMLFormElement;
  const input = document.createElement("input");
  input.type = "text";
  input.className = "chart-edit-input";
  input.placeholder = placeholder;

  const mic = micButton({
    onText: (text) => {
      input.value = text;
    },
    // Speaking a question and then having to find the button is a strange
    // half-measure; finishing the sentence is the submit.
    onDone: (text) => onSubmit(text.trim()),
  });

  const submit = document.createElement("button");
  submit.type = "submit";
  submit.className = "btn btn-primary btn-small";
  submit.textContent = "Add";

  form.append(input);
  if (mic) form.append(mic);
  form.append(submit);
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const value = input.value.trim();
    if (value) onSubmit(value);
  });

  return { form, input };
}
