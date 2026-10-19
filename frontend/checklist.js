// Draft checklist: does the draft text cover what the ad asked for?
// Pure keyword / date / number heuristics, no API. Wrong in both directions sometimes
// (it is a nudge, not a verdict); it never edits the draft.
(function (root) {
  const MONTHS = 'jan(?:uary|uar)?|feb(?:ruary|ruar)?|m[aä]rz|mar(?:ch)?|apr(?:il)?|mai|may|jun[ie]?|jul[iy]?|aug(?:ust)?|sep(?:t|tember)?|okt(?:ober)?|oct(?:ober)?|nov(?:ember)?|dez(?:ember)?|dec(?:ember)?';
  const DATE_RE = new RegExp(`\\b\\d{1,2}[./]\\d{1,2}(?:[./]\\d{2,4})?\\b|\\b\\d{4}-\\d{2}-\\d{2}\\b|\\b(?:${MONTHS})\\b|sofort|immediately|from now|asap|ab jetzt|next month|n[aä]chsten? monat|flexib|\\bper \\d|\\bab (?:dem )?\\d|\\bfrom \\d`, 'i');
  const CATS = [
    { id: 'date',  ask: /move.?in|einzug|ab wann|verf[uü]gbar|available|start date|beginn|per wann|when (can|are|do) you|ab[- ]?datum|eintritt/i,
      has: t => DATE_RE.test(t), hint: 'Say when you can move in' },
    { id: 'duration', ask: /dauer|duration|how long|wie lange|befristet|long.?term|langfristig/i,
      has: t => /monat|month|jahr|year|long.?term|langfrist|unbefrist|permanent|dauerhaft|semester|zeitlich|l[aä]ngere? zeit|for a long time|for the long run/i.test(t), hint: 'Say how long you want to stay' },
    { id: 'intro', ask: /vorstell|introduc|about (you|yourself)|[uü]ber (dich|sich|euch)|tell us|erz[aä]hl|who you are|wer du|short intro|kurz\w* (intro|beschreib|portr)|motivation|schreib\w* (uns|ein)|bewerbung/i,
      has: t => t.length >= 100 && /\b(ich bin|i am|i'm|my name|mein name|ich heisse|ich studiere|i study|ich arbeite|i work)\b/i.test(t), hint: 'Add a short introduction of yourself' },
    { id: 'phone', ask: /telefon|phone|handy|mobile|nummer|number|whatsapp/i,
      has: t => /\+?\d[\d\s\/-]{7,}\d/.test(t) || /whatsapp|erreichbar|reach me/i.test(t), hint: 'Add your phone number' },
    { id: 'photo', ask: /foto|photo|bild\b|picture|image/i,
      has: t => /foto|photo|bild|picture|attach|anbei|beigef|angeh[aä]ngt/i.test(t), hint: 'Mention or attach a photo' },
    { id: 'age', ask: /\balter\b|\bage\b|jahrgang|geburt|how old|wie alt/i,
      has: t => /\b(?:1[89]|[2-6]\d)(?:[- ]?(?:j[aä]hrig|years?|jahre|yo\b|y\/o))|\b(?:i am|i'm|ich bin)\s+(?:1[89]|[2-6]\d)\b|\bage[: ]+\d\d/i.test(t), hint: 'Say how old you are' },
    { id: 'job', ask: /beruf|occupation|\bjob\b|studium|\bstudy|student|arbeit|\bwork|besch[aä]ftig|profession|was machst|what do you do/i,
      has: t => /student|studier|studium|\beth\b|master|bachelor|msc|bsc|arbeit|work|job|beruf|engineer|ingenieur/i.test(t), hint: 'Say what you study or do' },
    { id: 'smoker', ask: /nichtrauch|non.?smok|raucher|smok/i,
      has: t => /rauch|smok/i.test(t), hint: 'Say whether you smoke' },
    { id: 'lang', ask: /deutsch|german|englisch|english|franz[oö]s|french|sprach|language|speak/i,
      has: t => /deutsch|german|englisch|english|sprach|language|native|muttersprach|fluent|flie(?:ss|ß)end/i.test(t), hint: 'Mention your language skills' },
    { id: 'docs', ask: /referenz|reference|betreibung|auszug|salary|lohn|einkomm|income|proof|nachweis|document|unterlagen|\bcv\b|lebenslauf/i,
      has: t => /attach|anbei|beigef|auf anfrage|on request|can provide|kann.*vorlegen|vorlegen|nachweis|proof|referenz|reference|funding|finanz/i.test(t), hint: 'Mention these documents / references' },
  ];
  const STOP = new Set(('und oder der die das den dem des ein eine einen einem einer mit f[uü]r auf bei von nach ' +
    'sie wir ihr euch uns ihre eure deine dein mein meine dass wird werden sein haben have with from your that this ' +
    'what when will please bitte kurz kurze short about also auch noch nur gerne bitte message nachricht ' +
    'include angeben nennen mention').split(' '));
  const stems = s => (s.toLowerCase().match(/[a-zäöüß0-9]+/g) || [])
    .filter(w => /^\d+$/.test(w) ? true : w.length >= 4 && !STOP.has(w))
    .map(w => /^\d+$/.test(w) ? w : w.slice(0, 5));

  // Conditions of the flat or household ("no pets", "noise-sensitive people unsuitable") are
  // facts about the place, not something the applicant has to write. Only treated as one when
  // the sentence asks for nothing we know how to look for (CATS), so "Nichtraucher" or
  // "nur Studierende" stay applicant-side.
  const CONDITION_RE = /\b(?:no|without)\s+(?:pets?|animals?|cats?|dogs?|parties|party|elevator|lift|balcony|washing machine|dishwasher|tv)\b|\bkeine?n?\s+(?:haustiere?|tiere?|katzen?|hunde?|partys?|lift|aufzug|balkon|waschmaschine)\b|\bohne\s+(?:lift|aufzug|balkon|haustiere?)\b|unsuitable|not suitable|nicht geeignet|ungeeignet|\bnot (?:for|ideal for|recommended)\b|nicht empfohlen|l[aä]rmempfindlich|noise[- ]sensitive|ger[aä]uschempfindlich|hell?h[oö]rig|\bstairs\b|treppe|\bwalk[- ]?up\b|\bnoisy\b|\bbusy (?:street|road)\b|hauptstrasse/i;
  // 'mention' (applicant should address it) or 'condition' (a fact about the flat).
  function classify(text, kind) {
    if (kind !== 'req') return 'mention';
    const t = String(text || '');
    if (CATS.some(c => c.ask.test(t))) return 'mention';
    return CONDITION_RE.test(t) ? 'condition' : 'mention';
  }

  // The extracted fields (move_in, duration) make date / duration items tick more reliably.
  function fieldOk(cat, text, ext) {
    if (!ext) return false;
    if (cat.id === 'date' && /^\d{4}-\d{2}-\d{2}$/.test(ext.move_in || '')) {
      const parts = ext.move_in.split('-').map(Number);
      return new RegExp(`\\b0?${parts[2]}[./]\\s?0?${parts[1]}\\b`).test(text);   // 1.11. / 01/11
    }
    if (cat.id === 'duration' && ext.duration === 'permanent') return /unbefrist|long|dauer|lang/i.test(text);
    return false;
  }

  // The ad's asks (asks_applicant_to_include) and hard requirements as checklist items.
  function items(ext) {
    const out = [];
    for (const t of (ext && ext.asks_applicant_to_include) || []) if (t) out.push({ text: String(t), kind: 'ask' });
    for (const t of (ext && ext.hard_requirements) || []) if (t && classify(t, 'req') === 'mention') out.push({ text: String(t), kind: 'req' });
    return out;
  }

  // -> { ok: bool, hint: string }
  function check(item, draftText, ext) {
    const text = String(draftText || '');
    const cats = CATS.filter(c => c.ask.test(item.text));
    if (cats.some(c => c.has(text) || fieldOk(c, text, ext))) return { ok: true, hint: '' };
    const need = stems(item.text);
    if (need.length) {
      const words = (text.toLowerCase().match(/[a-zäöüß0-9]+/g) || []);
      const hit = need.filter(s => /^\d+$/.test(s) ? words.includes(s) : words.some(w => w.startsWith(s)));
      // long requirement sentences: a third of the key words is enough
      const frac = item.kind === 'req' && need.length > 4 ? 3 : 2;
      if (hit.length >= Math.max(1, Math.ceil(need.length / frac)) && !cats.length) return { ok: true, hint: '' };
      if (hit.length === need.length) return { ok: true, hint: '' };
    }
    const hint = cats.length ? cats[0].hint
      : item.kind === 'req' ? 'Not addressed in the draft — mention it only if it applies to you' : 'Not mentioned in your draft yet';
    return { ok: false, hint };
  }

  const api = { items, check, classify };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.WGChecklist = api;
})(typeof window !== 'undefined' ? window : globalThis);
