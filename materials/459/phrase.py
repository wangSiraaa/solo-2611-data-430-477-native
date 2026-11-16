def search(index,terms):
 if not terms:return []
 candidates=set(index.postings(terms[0]))
 for i,term in enumerate(terms[1:],1):
  later=set(index.postings(term));candidates={(d,p) for d,p in candidates if (d,p+i) in later}
 return sorted({d for d,p in candidates})
