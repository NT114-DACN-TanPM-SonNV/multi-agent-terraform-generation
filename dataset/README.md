# Dataset — Terraform IaC Generation Benchmarks

Benchmark datasets for evaluating the Terraform HCL generation pipeline from natural language requests.

## 📊 Overview

| Dataset | Cases | Size | Purpose |
|---------|-------|------|---------|
| **data-dev.csv** | 34 | 89 KB | Quick development & validation |
| **data-test.csv** | 140 | 340 KB | Comprehensive evaluation & benchmarking |

**Total: 174 test cases**

---

## 🏗️ Structure

Each row is a test case with the following columns:

| Column | Description |
|--------|-------------|
| **Resource** | AWS resource types to be created (e.g., `aws_route53_record, aws_vpc`) |
| **Prompt** | User request (natural language) |
| **Rego intent** | Expected validation rules (Rego) |
| **Difficulty** | Difficulty level: 1=simple, 6=complex |
| **Reference output** | Ground truth Terraform HCL (golden standard) |
| **Intent** | Summary of expected behavior |

---

**Note:** Top Services & Resources below are aggregated from both datasets (total 174 cases).

## 🔗 Top AWS Services

| Service | Resources | Coverage | Use Cases |
|---------|-----------|----------|-----------|
| **IAM** | aws_iam_role, aws_iam_policy_* | 52.9% | Access control, permissions |
| **Storage (S3)** | aws_s3_bucket | 36.8% | Blob storage, static hosting |
| **Networking (VPC)** | aws_vpc, aws_subnet | 24.1% | Network isolation, subnets |
| **DNS (Route53)** | aws_route53_* | 14.9% | Domain management |
| **Compute (Lambda)** | aws_lambda_function, aws_lambda_permission | 14.9% | Serverless functions |
| **Databases** | aws_dynamodb_table | 10.3% | NoSQL tables |
| **CI/CD** | aws_codebuild_project | 9.8% | Build pipelines |
| **Compute (EC2)** | aws_instance | 5.7% | Virtual machines |

---

## 🏆 Top AWS Resources

| Rank | Resource | Count | Frequency |
|------|----------|-------|-----------|
| 1 | `aws_s3_bucket` | 64 | 36.8% |
| 2 | `aws_iam_role` | 48 | 27.6% |
| 3 | `aws_iam_policy_document` | 44 | 25.3% |
| 4 | `aws_vpc` | 30 | 17.2% |
| 5 | `aws_lambda_function` | 22 | 12.6% |
| 6 | `aws_iam_role_policy_attachment` | 19 | 10.9% |
| 7 | `aws_dynamodb_table` | 18 | 10.3% |
| 8 | `aws_codebuild_project` | 17 | 9.8% |
| 9 | `archive_file` | 15 | 8.6% |
| 10 | `aws_route53_record` | 14 | 8.0% |

Also present: `aws_lambda_permission`, `aws_route53_zone`, `aws_subnet`, `aws_instance`, `aws_lightsail_instance`, `aws_iam_policy`.

---

## 📈 Difficulty Distribution

```
Difficulty 1  → Single resource, minimal dependencies
Difficulty 2  → 2-3 resources, basic config
Difficulty 3  → 3-5 resources, some dependencies
Difficulty 4  → 5-7 resources, moderate complexity
Difficulty 5  → 7+ resources, complex IAM/networking
Difficulty 6  → 10+ resources, multi-service orchestration
```

**Distribution:**

| Difficulty | data-dev | % | data-test | % |
|------------|----------|---|-----------|---|
| 1 | 3 | 8.8% | 17 | 12.1% |
| 2 | 11 | 32.4% | 48 | 34.3% |
| 3 | 11 | 32.4% | 39 | 27.9% |
| 4 | 2 | 5.9% | 16 | 11.4% |
| 5 | 5 | 14.7% | 14 | 10.0% |
| 6 | 2 | 5.9% | 6 | 4.3% |

---

## 🚀 Usage

```bash
# Dev dataset (fast, for testing)
python evaluate.py --csv dataset/data-dev.csv --limit 10

# Full test dataset (slow, for evaluation)
python evaluate.py --csv dataset/data-test.csv --workers 4

# Specific cases
python evaluate.py --csv dataset/data-dev.csv --cases 0 5 10
```

---

## 🙏 Attribution

**Original Dataset:** [AutoIAC Project - IaC Evaluation Dataset](https://huggingface.co/datasets/autoiac-project/iac-eval)

These datasets are derived from the **autoiac-project/iac-eval** benchmark on Hugging Face. The original work provides comprehensive Infrastructure-as-Code evaluation benchmarks across multiple cloud providers and languages.

If you use this dataset, please cite the original AutoIAC project to respect the authors' work.
