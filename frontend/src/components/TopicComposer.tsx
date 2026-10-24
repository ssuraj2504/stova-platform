import { useRef } from "react";
import { ArrowDown, ArrowUp, GripVertical, Plus, X } from "lucide-react";

export type TopicInput = { id: string; title: string; tag: string; constraints: string };
type Props = { topics: TopicInput[]; onChange: (topics: TopicInput[]) => void; locked: boolean; existing: boolean };

export default function TopicComposer({ topics, onChange, locked, existing }: Props) {
  const dragged = useRef<number | null>(null);
  function move(from: number, to: number) {
    if (locked || from === to || to < 0 || to >= topics.length) return;
    const next = [...topics];
    const [topic] = next.splice(from, 1);
    next.splice(to, 0, topic);
    onChange(next);
  }
  function update(index: number, key: "title" | "tag" | "constraints", value: string) {
    onChange(topics.map((topic, i) => i === index ? { ...topic, [key]: value } : topic));
  }
  return <div className="journal-topic-composer">
    <div className="journal-topic-list">
      {topics.map((topic, index) => <div className="journal-topic-row" key={topic.id}
        onDragOver={event => { if (!locked) event.preventDefault(); }}
        onDrop={event => { event.preventDefault(); if (dragged.current !== null) move(dragged.current, index); dragged.current = null; }}>
        <div className="journal-topic-main">
          <button type="button" className="journal-drag-handle" draggable={!locked} disabled={locked} aria-label={`Drag topic ${index + 1} to reorder`}
            onDragStart={event => { dragged.current = index; event.dataTransfer.effectAllowed = "move"; event.dataTransfer.setData("text/plain", topic.id); }} onDragEnd={() => { dragged.current = null; }}><GripVertical size={15} /></button>
          <label className="journal-topic-number" htmlFor={`topic-${topic.id}`}>{String(index + 1).padStart(2, "0")}</label>
          <input id={`topic-${topic.id}`} value={topic.title} maxLength={200} disabled={locked} placeholder={index === 0 ? "What would you like to write about?" : "Add another article topic"} onChange={event => update(index, "title", event.target.value)} />
          {!existing && <button className="journal-icon-button" type="button" disabled={locked || topics.length === 1} onClick={() => onChange(topics.filter((_, i) => i !== index))} aria-label={`Remove topic ${index + 1}`}><X size={15} /></button>}
        </div>
        <div className="journal-topic-lower"><details className="journal-topic-details"><summary>{topic.tag ? <span className="journal-chip">{topic.tag}</span> : "Tag & writing notes"}{topic.constraints && <span className="journal-topic-note-indicator">Notes added</span>}</summary><div className="journal-field"><label htmlFor={`tag-${topic.id}`}>Topic tag <span>Optional</span></label><input id={`tag-${topic.id}`} value={topic.tag} maxLength={80} disabled={locked} placeholder="e.g. Strategy" onChange={event => update(index, "tag", event.target.value)} /></div><div className="journal-field"><label htmlFor={`constraints-${topic.id}`}>Writing notes <span>Optional</span></label><textarea id={`constraints-${topic.id}`} value={topic.constraints} maxLength={1000} rows={2} disabled={locked} placeholder="Specific points to cover, examples to use, or things to avoid." onChange={event => update(index, "constraints", event.target.value)} /></div></details>
          <div className="journal-topic-actions"><button type="button" className="journal-icon-button" disabled={locked || index === 0} aria-label={`Move topic ${index + 1} up`} onClick={() => move(index, index - 1)}><ArrowUp size={13} /></button><button type="button" className="journal-icon-button" disabled={locked || index === topics.length - 1} aria-label={`Move topic ${index + 1} down`} onClick={() => move(index, index + 1)}><ArrowDown size={13} /></button></div></div>
      </div>)}
    </div>
    {!existing && <button type="button" className="journal-add-topic" disabled={locked || topics.length >= 10} onClick={() => onChange([...topics, { id: crypto.randomUUID(), title: "", tag: "", constraints: "" }])}><Plus size={15} />Add a topic<span>{topics.length} / 10</span></button>}
    {existing && !locked && <p className="journal-help-text">You can rename and reorder these topics before writing. Create a new publication for a different set.</p>}
    {locked && <p className="journal-help-text">Topics are fixed once writing begins. You can edit the finished article in the editor.</p>}
  </div>;
}
