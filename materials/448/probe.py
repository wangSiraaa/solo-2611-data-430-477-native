import json
from scan import run
expr={'op':'eq','col':'x','param':0}
print(json.dumps({'normal':run('seed',expr,[3]),'default_key':run('seed',expr,[30]),'oracle':run('seed',expr,[30],False)}))
