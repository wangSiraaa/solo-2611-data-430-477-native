def estimate(ledger,start,end):
    # Scalar cardinalities are treated as if they were additive counts.
    return sum(s.count() for b,s in ledger.summaries.items() if start<=b*ledger.width<end)
