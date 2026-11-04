"""
Tests for the schema_loader module.
"""
import pytest
import tempfile
import os
import json
import yaml
from unittest.mock import patch, mock_open, MagicMock

from syda.schema_loader import SchemaLoader


@pytest.fixture
def sample_schema_dict():
    """Return a sample schema dictionary."""
    return {
        "id": {"type": "number", "description": "Unique identifier"},
        "name": {"type": "text", "description": "Customer name"},
        "email": {"type": "email", "description": "Customer email"},
        "__description__": "Customer schema"
    }


@pytest.fixture
def sample_schema_with_fk():
    """Return a sample schema with foreign key."""
    return {
        "id": {"type": "number", "description": "Unique identifier"},
        "customer_id": {
            "type": "foreign_key", 
            "description": "Reference to customer", 
            "references": {"schema": "Customer", "field": "id"}
        },
        "__foreign_keys__": {
            "customer_id": ["Customer", "id"]
        },
        "__depends_on__": ["Customer"]
    }


class TestSchemaLoader:
    """Tests for the SchemaLoader class."""
    
    def test_load_schema_dict(self, sample_schema_dict):
        """Test loading schema from a dictionary."""
        loader = SchemaLoader()
        
        schema, metadata, desc, fks, template, depends_on = loader.load_schema(sample_schema_dict)
        
        # Check the schema is properly processed
        assert "id" in schema
        # The SchemaLoader extracts just the type field, not the entire dictionary
        assert schema["id"] == "number"
        # The field metadata gets moved to the metadata dictionary
        assert "id" in metadata
        assert "description" in metadata["id"]
        assert metadata["id"]["description"] == "Unique identifier"
        # Check other return values
        assert desc == "Customer schema"
        assert fks == {}  # No foreign keys
        assert template == {}  # No templates
        assert depends_on == []  # No dependencies
        
    def test_load_schema_with_foreign_keys(self, sample_schema_with_fk):
        """Test loading schema with foreign keys."""
        loader = SchemaLoader()
        
        schema, metadata, desc, fks, template, depends_on = loader.load_schema(sample_schema_with_fk)
        
        # Check that foreign keys were extracted
        assert "customer_id" in fks
        assert fks["customer_id"] == ("Customer", "id")
        
        # Check dependencies were identified
        assert "Customer" in depends_on
        
    def test_load_schema_json_file(self, sample_schema_dict):
        """Test loading schema from a JSON file."""
        # Create a temporary JSON file
        with tempfile.NamedTemporaryFile(suffix='.json', delete=False) as tmp:
            tmp.write(json.dumps(sample_schema_dict).encode('utf-8'))
            tmp_path = tmp.name
        
        try:
            # Load the schema from the file
            loader = SchemaLoader()
            schema, metadata, desc, fks, template, depends_on = loader.load_schema(tmp_path)
            
            # Check the schema is properly loaded
            assert "id" in schema
            assert schema["id"] == "number"
            assert "id" in metadata
            assert "description" in metadata["id"]
            assert metadata["id"]["description"] == "Unique identifier"
        finally:
            # Clean up
            os.unlink(tmp_path)
            
    def test_load_schema_yaml_file(self, sample_schema_dict):
        """Test loading schema from a YAML file."""
        # Create a temporary YAML file
        with tempfile.NamedTemporaryFile(suffix='.yaml', delete=False) as tmp:
            tmp.write(yaml.dump(sample_schema_dict).encode('utf-8'))
            tmp_path = tmp.name
        
        try:
            # Load the schema from the file
            loader = SchemaLoader()
            schema, metadata, desc, fks, template, depends_on = loader.load_schema(tmp_path)
            
            # Check the schema is properly loaded
            assert "id" in schema
            assert schema["id"] == "number"
            assert "id" in metadata
            assert "description" in metadata["id"]
            assert metadata["id"]["description"] == "Unique identifier"
        finally:
            # Clean up
            os.unlink(tmp_path)
            
    def test_load_schema_unsupported_file_type(self):
        """Test error handling for unsupported file type."""
        # Create a temporary file with unsupported extension
        with tempfile.NamedTemporaryFile(suffix='.txt', delete=False) as tmp:
            tmp.write(b"Not a schema file")
            tmp_path = tmp.name
        
        try:
            # Try to load the schema
            loader = SchemaLoader()
            with pytest.raises(ValueError, match="Unsupported schema file type"):
                loader.load_schema(tmp_path)
        finally:
            # Clean up
            os.unlink(tmp_path)
            
    @patch('builtins.open', new_callable=mock_open)
    def test_load_schema_file_not_found(self, mock_file):
        """Test error handling when file is not found."""
        # Mock the open function to raise FileNotFoundError
        mock_file.side_effect = FileNotFoundError
        
        loader = SchemaLoader()
        with pytest.raises(ValueError, match="Schema file not found"):
            loader.load_schema("nonexistent_file.json")
            
    def test_load_schema_sqlalchemy_model(self):
        """Test loading schema from SQLAlchemy model."""
        from sqlalchemy import Column, Integer, String
        from sqlalchemy.orm import declarative_base

        Base = declarative_base()

        class User(Base):
            __tablename__ = "users"
            id = Column(Integer, primary_key=True)
            name = Column(String(50))

        loader = SchemaLoader()
        raw_dict = loader._load_sqlalchemy_model(User)
        assert raw_dict["__table_name__"] == "users"

        schema, metadata, desc, fks, template, depends_on = loader.load_schema(User)
        assert "id" in schema
        assert "name" in schema
        assert metadata["id"]["constraints"]["primary_key"] is True
