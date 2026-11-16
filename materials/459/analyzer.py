def analyze(tokens):
 arcs=[{'start':i,'end':i+1,'term':t,'source':[i,i+1]} for i,t in enumerate(tokens)]
 for i in range(len(tokens)-1):
  if tokens[i:i+2]==['new','york']:arcs.append({'start':i,'end':i+2,'term':'nyc','source':[i,i+2]})
 return arcs
