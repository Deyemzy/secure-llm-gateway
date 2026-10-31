output "vpc_id" {
  value = aws_vpc.this.id
}

output "vpc_cidr" {
  value = aws_vpc.this.cidr_block
}

output "public_subnet_ids" {
  value = aws_subnet.public[*].id
}

output "private_subnet_ids" {
  value = aws_subnet.private[*].id
}

output "endpoints_security_group_id" {
  value = aws_security_group.endpoints.id
}

output "bedrock_vpce_id" {
  value = aws_vpc_endpoint.interface["bedrock-runtime"].id
}

output "s3_prefix_list_id" {
  value = aws_vpc_endpoint.s3.prefix_list_id
}
