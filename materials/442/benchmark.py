import hashlib,json,sys

def rows():
    words={hashlib.sha256(f'lexicon-{i}'.encode()).hexdigest()[:12] for i in range(9996)}
    words|={'abcdef123456','abcdef123457','abcdef12345','abcxef123456'}
    assert len(words)==10000
    return [{'id':str(i),'term':word,'freq':int(hashlib.sha256(word.encode()).hexdigest()[-4:],16)%1000} for i,word in enumerate(sorted(words))]
if __name__=='__main__':print(json.dumps({'rows':rows(),'query':'abcdef123456','max_edits':2,'k':20},ensure_ascii=False))
