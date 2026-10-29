       IDENTIFICATION DIVISION.
       PROGRAM-ID. PAYROLL.
       ENVIRONMENT DIVISION.
       INPUT-OUTPUT SECTION.
       FILE-CONTROL.
           SELECT EMP-FILE ASSIGN TO 'EMPLOYEES.DAT'.
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       01 WS-HOURS        PIC 9(3).
       01 WS-RATE         PIC 9(3)V99 VALUE 18.50.
       01 WS-PAY          PIC 9(7)V99.
       01 WS-EOF          PIC X VALUE 'N'.
       PROCEDURE DIVISION.
      * Entry point: pays every employee in the file.
       MAIN-PARA.
           OPEN INPUT EMP-FILE.
           PERFORM READ-EMP UNTIL WS-EOF = 'Y'.
           CLOSE EMP-FILE.
           STOP RUN.
       READ-EMP.
           READ EMP-FILE AT END MOVE 'Y' TO WS-EOF.
           PERFORM CALC-PAY.
       CALC-PAY.
           IF WS-HOURS > 40
              COMPUTE WS-PAY = 40 * WS-RATE + (WS-HOURS - 40) * WS-RATE * 1.5
           ELSE
              COMPUTE WS-PAY = WS-HOURS * WS-RATE
           END-IF.
