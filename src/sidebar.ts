// The recent-files sidebar. Rust keeps the list and checks which files still exist; this draws it and
// reports clicks. File names go in as text, never as HTML.

export interface RecentFile {
  path: string;
  name: string;
  dir: string;
  exists: boolean;
  current: boolean;
}

interface Actions {
  open(file: RecentFile): void;
  remove(file: RecentFile): void;
}

// Builds the sidebar in `root` and returns the function that shows a new list.
export function createSidebar(root: HTMLElement, actions: Actions): (files: RecentFile[]) => void {
  const list = element("ul");
  const empty = element("p", "Files you open appear here.", "empty");
  root.append(element("h2", "Recent"), list, empty);
  let files: RecentFile[] = [];

  list.addEventListener("click", (event) => {
    const button = (event.target as Element).closest("button");
    const file = files[Number(button?.closest("li")?.dataset.index)];
    if (!button || !file) return;
    if (button.classList.contains("remove")) actions.remove(file);
    else actions.open(file);
  });

  return (next) => {
    files = next;
    empty.hidden = files.length > 0;
    list.replaceChildren(...files.map(item));
  };
}

function item(file: RecentFile, index: number): HTMLLIElement {
  const li = element("li");
  li.dataset.index = String(index);
  li.classList.toggle("current", file.current);
  li.classList.toggle("missing", !file.exists);

  const open = element("button", undefined, "file");
  open.title = file.exists ? file.path : `${file.path} was moved or deleted`;
  open.append(element("span", file.name, "name"), element("span", file.exists ? file.dir : "Moved or deleted", "dir"));
  if (file.current) open.setAttribute("aria-current", "page");

  const remove = element("button", "×", "remove");
  remove.title = "Remove from the list";
  remove.setAttribute("aria-label", `Remove ${file.name} from the list`);

  li.append(open, remove);
  return li;
}

function element<K extends keyof HTMLElementTagNameMap>(tag: K, text?: string, className?: string) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}
