import json,apsw

def choose(info):
    choices=[];argv=1
    for i in range(info.nConstraint):
        column=info.get_aConstraint_iColumn(i);op=info.get_aConstraint_op(i)
        if info.get_aConstraint_usable(i) and op==apsw.SQLITE_INDEX_CONSTRAINT_EQ and column in (0,1):
            info.set_aConstraintUsage_argvIndex(i,argv);info.set_aConstraintUsage_omit(i,True);choices.append(column);argv+=1
    info.idxStr=json.dumps(choices);info.estimatedCost=1.0 if 0 in choices else 100.0
    if info.nOrderBy==1 and info.get_aOrderBy_iColumn(0)==2 and not info.get_aOrderBy_desc(0):info.orderByConsumed=True
    return True
