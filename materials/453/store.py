import sqlite3

def create():
 db=sqlite3.connect(':memory:');db.executescript('CREATE TABLE facts(id TEXT PRIMARY KEY,g TEXT,n INTEGER);CREATE TABLE totals(g TEXT PRIMARY KEY,n INTEGER);CREATE TABLE audit(g TEXT,n INTEGER);');return db
