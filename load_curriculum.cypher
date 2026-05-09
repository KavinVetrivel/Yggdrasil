// ============================================================
// Neo4j Load Script — CSE AI&ML Curriculum Knowledge Graph
// Run these in Neo4j Browser or via neo4j-shell
// Assumes subjects.csv is in Neo4j import directory
// ============================================================

// 1. Constraints (run once)
CREATE CONSTRAINT subject_code IF NOT EXISTS FOR (s:Subject) REQUIRE s.code IS UNIQUE;
CREATE CONSTRAINT semester_num IF NOT EXISTS FOR (sem:Semester) REQUIRE sem.number IS UNIQUE;

// 2. Create Program node
MERGE (:Program {code: 'CSE_AIML', name: 'BE Computer Science and Engineering (AI&ML)', regulation: '2023', total_credits: 166});

// 3. Create Semester nodes
UNWIND range(1,8) AS n
MERGE (:Semester {number: n});

// 4. Link Semesters to Program
MATCH (p:Program {code: 'CSE_AIML'})
MATCH (sem:Semester)
MERGE (sem)-[:PART_OF]->(p);

// 5. Load Subjects from CSV
// NOTE: Place subjects.csv in your Neo4j import folder first
// On Neo4j Desktop: open the DB folder > import > paste subjects.csv there
LOAD CSV WITH HEADERS FROM 'file:///subjects.csv' AS row
MERGE (s:Subject {code: row.code})
SET s.name          = row.name,
    s.credits       = toInteger(row.credits),
    s.lecture_hrs   = toInteger(row.lecture_hrs),
    s.tutorial_hrs  = toInteger(row.tutorial_hrs),
    s.practical_hrs = toInteger(row.practical_hrs),
    s.category      = row.category,
    s.type          = row.type,
    s.semester      = toInteger(row.semester);

// 6. Link Subjects to Semesters
MATCH (s:Subject)
MATCH (sem:Semester {number: s.semester})
MERGE (s)-[:BELONGS_TO]->(sem);

// ============================================================
// VERIFY — run these after loading
// ============================================================

// Check node counts
MATCH (s:Subject) RETURN count(s) AS total_subjects;
MATCH (sem:Semester) RETURN sem.number, count{(s:Subject)-[:BELONGS_TO]->(sem)} AS subject_count ORDER BY sem.number;

// Browse all subjects in a semester
MATCH (s:Subject)-[:BELONGS_TO]->(sem:Semester {number: 1})
RETURN s.code, s.name, s.credits, s.category, s.type ORDER BY s.code;

// Check full graph
MATCH (p:Program)<-[:PART_OF]-(sem:Semester)<-[:BELONGS_TO]-(s:Subject)
RETURN p.name, sem.number, count(s) ORDER BY sem.number;
