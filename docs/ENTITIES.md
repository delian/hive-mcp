# Entities

People, systems, parts and concepts that several notes talk about. An entity is an ordinary item
(`kind=entity`): it has a scope (private by default, or a project), is searchable, can be linked, edited, merged
(`kb_merge`) and purged. Its name and aliases are stored as `alias/<slug>` tags and its type as `entity/<type>`.

| Tool | |
|---|---|
| `kb_entity_resolve(name, type?)` | is it already known? Matches name or any alias ignoring case, spacing and punctuation (`Pulse-Doppler radar` = `pulse doppler radar`; `C++`, `C#` and `C` stay different); falls back to close spellings (`similar`). Other groups' entities appear marked external |
| `kb_entity_save(name, type, description?, aliases?, share_with_project?)` | create it, or - if the *name* is already one of yours - add the aliases/description to it |
| `kb_entity_mention(id, entities)` | link a note to entities (ids or names) with `mentions`; `kb_graph(entity, relations=["mentions"])` then lists every note that mentions it |

Rules that prevent duplicates and mix-ups:
- Only the **name** decides "same entity". An alias that already belongs to another of your entities is reported in
  `conflicts` and is not stolen; to merge two entities, use `kb_merge` on them.
- Entities of other groups are never edited. If you need your own view of one, save your own entity (and link it
  `derived_from`); if the name belongs to an entity of your own project that you cannot edit, saving is refused.
- `kb_ingest` links each stored part to **your own** entities whose name or alias (3+ characters) appears in its
  text. Pass `link_entities=false` to skip.
Not done: automatic discovery of new entities (the server has no language model; the agent proposes them), and
entity types are free text (no schema).
