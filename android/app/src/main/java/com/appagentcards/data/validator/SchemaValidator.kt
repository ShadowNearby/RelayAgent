package com.appagentcards.data.validator

import com.fasterxml.jackson.databind.JsonNode
import com.fasterxml.jackson.databind.ObjectMapper
import com.networknt.schema.JsonSchemaFactory
import com.networknt.schema.SpecVersion
import javax.inject.Inject
import javax.inject.Singleton

@Singleton
class SchemaValidator @Inject constructor() {

    private val objectMapper = ObjectMapper()

    fun validate(yamlJson: String, schemaJson: String): ValidationResult {
        return try {
            val schemaNode: JsonNode = objectMapper.readTree(schemaJson)
            val manifestNode: JsonNode = objectMapper.readTree(yamlJson)

            val factory = JsonSchemaFactory.getInstance(SpecVersion.VersionFlag.V7)
            val schema = factory.getSchema(schemaNode)
            val errors = schema.validate(manifestNode)

            if (errors.isEmpty()) {
                ValidationResult.Valid
            } else {
                ValidationResult.Invalid(errors.map { it.message })
            }
        } catch (e: Exception) {
            ValidationResult.Invalid(listOf(e.message ?: "Unknown validation error"))
        }
    }
}

sealed class ValidationResult {
    object Valid : ValidationResult()
    data class Invalid(val errors: List<String>) : ValidationResult()
}
