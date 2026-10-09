# PDD-FIN-MINI-001 Supplier Master Check

## 1 Document Control

| Field | Value |
|---|---|
| Document ID | PDD-FIN-MINI-001 |
| Version | 1.0 |
| Status | Approved |
| Process owner | Finance Operations |

### 1.1 Approval and Sign-off

| Role | Name | Decision | Date |
|---|---|---|---|
| Process owner | Marco Rossi | Approved | 2026-09-30 |
| RPA CoE | Anna Keller | Approved | 2026-10-01 |

## 2 Purpose

The robot checks new supplier records in the ERP against the sanctions list and writes the
result to the tracking workbook.

## 3 Applications

| Application | Type | Access |
|---|---|---|
| ERP Desktop Client | Windows desktop | Windows VDI |
| Microsoft Excel | Office | Microsoft 365 Apps |

## 4 Process steps

| Step ID | Name | Action | Application |
|---|---|---|---|
| S01 | Load configuration | Read the Config workbook and assets. | Orchestrator |
| S02 | Open supplier record | Open the supplier in the ERP client and read the VAT number. | ERP Desktop Client |
| S03 | Write result | Append the check result to the tracking workbook. | Microsoft Excel |

## 5 Business rules

| ID | Rule |
|---|---|
| BR-01 | A supplier without a VAT number is a business exception. |

- Volumes: about 40 suppliers per day.
- Schedule: every working day at 06:00.
