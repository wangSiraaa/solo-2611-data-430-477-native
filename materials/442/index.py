def candidates(lexicon,query):
    rows=list(lexicon.db.execute('select id,term,freq from buckets where prefix=? order by term',(query[:2],)));lexicon.reads+=len(rows);return rows
