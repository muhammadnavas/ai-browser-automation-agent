import React, { useState, useEffect, useRef } from 'react';
import {
  User,
  Folder,
  Brain,
  PlusCircle,
  CheckCircle,
  AlertCircle,
  X,
  Save,
  Trash2,
  Edit3,
  ArrowLeft,
  Shield,
  Loader2,
  UploadCloud,
  FileText,
  File,
  Eye,
  Download,
  Copy,
  Check,
  Search,
} from 'lucide-react';

export default function MyContext({ authFetch, api, onClose }) {
  const [activeTab, setActiveTab] = useState('profile');

  // Profile state
  const [profile, setProfile] = useState({
    personal_details: { full_name: '', email: '', phone: '', location: '' },
    education: { college: '', degree: '', graduation_year: '', cgpa: '' },
    skills_and_experience: { technical_skills: '', internships: '', projects: '' },
    career_interests: '',
    job_preferences: '',
    custom_instructions: '',
  });

  // Documents state
  const [documents, setDocuments] = useState([]);
  const [isUploading, setIsUploading] = useState(false);
  const [dragOver, setDragOver] = useState(false);
  const [viewingDocText, setViewingDocText] = useState(null);
  const [copiedText, setCopiedText] = useState(false);
  const fileInputRef = useRef(null);

  // Memory state
  const [memories, setMemories] = useState([]);
  const [selectedCategoryFilter, setSelectedCategoryFilter] = useState('All');
  const [newMemoryContent, setNewMemoryContent] = useState('');
  const [newMemoryCategory, setNewMemoryCategory] = useState('Personal');
  const [editingMemory, setEditingMemory] = useState(null);
  const [isAddingMemory, setIsAddingMemory] = useState(false);

  // Common UI state
  const [isLoading, setIsLoading] = useState(true);
  const [isSaving, setIsSaving] = useState(false);
  const [error, setError] = useState(null);
  const [successMessage, setSuccessMessage] = useState(null);
  const [dbAvailable, setDbAvailable] = useState(true);

  // Edit modal state (Profile)
  const [editingSection, setEditingSection] = useState(null);
  const [editFormData, setEditFormData] = useState({});

  useEffect(() => {
    if (activeTab === 'profile') {
      fetchProfile();
    } else if (activeTab === 'documents') {
      fetchDocuments();
    } else if (activeTab === 'memory') {
      fetchMemories();
    }
  }, [activeTab]);

  // --------------------------------------------------------------------------
  // Profile Handlers
  // --------------------------------------------------------------------------

  const fetchProfile = async () => {
    setIsLoading(true);
    setError(null);
    try {
      const res = await authFetch(api('/api/context/profile'));
      if (res.ok) {
        const data = await res.json();
        setDbAvailable(true);
        if (data.profile_data) {
          setProfile((prev) => ({
            ...prev,
            ...data.profile_data,
            personal_details: { ...prev.personal_details, ...(data.profile_data.personal_details || {}) },
            education: { ...prev.education, ...(data.profile_data.education || {}) },
            skills_and_experience: { ...prev.skills_and_experience, ...(data.profile_data.skills_and_experience || {}) },
          }));
        }
      } else if (res.status === 503) {
        setDbAvailable(false);
        setError('Neon PostgreSQL database is currently unreachable. Please check DATABASE_URL and network connectivity.');
      } else {
        const err = await res.json().catch(() => ({}));
        setError(err.detail || 'Failed to load profile data.');
      }
    } catch (err) {
      setError('Network error while connecting to My Context service.');
    } finally {
      setIsLoading(false);
    }
  };

  const handleOpenEdit = (section) => {
    setEditingSection(section);
    if (section === 'personal_details') {
      setEditFormData({ ...(profile.personal_details || {}) });
    } else if (section === 'education') {
      setEditFormData({ ...(profile.education || {}) });
    } else if (section === 'skills_and_experience') {
      setEditFormData({ ...(profile.skills_and_experience || {}) });
    } else if (section === 'custom_info') {
      setEditFormData({
        career_interests: profile.career_interests || '',
        job_preferences: profile.job_preferences || '',
        custom_instructions: profile.custom_instructions || '',
      });
    }
  };

  const handleSaveEdit = async () => {
    setIsSaving(true);
    setError(null);
    setSuccessMessage(null);

    const updatedProfile = { ...profile };
    if (editingSection === 'personal_details') {
      updatedProfile.personal_details = { ...editFormData };
    } else if (editingSection === 'education') {
      updatedProfile.education = { ...editFormData };
    } else if (editingSection === 'skills_and_experience') {
      updatedProfile.skills_and_experience = { ...editFormData };
    } else if (editingSection === 'custom_info') {
      updatedProfile.career_interests = editFormData.career_interests || '';
      updatedProfile.job_preferences = editFormData.job_preferences || '';
      updatedProfile.custom_instructions = editFormData.custom_instructions || '';
    }

    try {
      const res = await authFetch(api('/api/context/profile'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ profile_data: updatedProfile }),
      });

      if (res.ok) {
        setProfile(updatedProfile);
        setSuccessMessage('Profile information saved successfully.');
        setEditingSection(null);
        setTimeout(() => setSuccessMessage(null), 3500);
      } else {
        const err = await res.json().catch(() => ({}));
        setError(err.detail || 'Failed to save profile.');
      }
    } catch (err) {
      setError('Network error saving profile changes.');
    } finally {
      setIsSaving(false);
    }
  };

  const handleDeleteProfile = async () => {
    if (!window.confirm('Are you sure you want to delete your persistent profile? This action cannot be undone.')) {
      return;
    }
    setIsSaving(true);
    try {
      const res = await authFetch(api('/api/context/profile'), {
        method: 'DELETE',
      });
      if (res.ok) {
        setProfile({
          personal_details: { full_name: '', email: '', phone: '', location: '' },
          education: { college: '', degree: '', graduation_year: '', cgpa: '' },
          skills_and_experience: { technical_skills: '', internships: '', projects: '' },
          career_interests: '',
          job_preferences: '',
          custom_instructions: '',
        });
        setEditingSection(null);
        setSuccessMessage('Profile deleted successfully.');
        setTimeout(() => setSuccessMessage(null), 3500);
      } else {
        setError('Failed to delete profile.');
      }
    } catch (err) {
      setError('Network error deleting profile.');
    } finally {
      setIsSaving(false);
    }
  };

  // --------------------------------------------------------------------------
  // Document Vault Handlers
  // --------------------------------------------------------------------------

  const fetchDocuments = async () => {
    setIsLoading(true);
    setError(null);
    try {
      const res = await authFetch(api('/api/context/documents'));
      if (res.ok) {
        const data = await res.json();
        setDocuments(data || []);
      } else if (res.status === 503) {
        setError('PostgreSQL database is currently unavailable.');
      } else {
        setError('Failed to load documents.');
      }
    } catch (err) {
      setError('Network error loading document vault.');
    } finally {
      setIsLoading(false);
    }
  };

  const handleFileUpload = async (file) => {
    if (!file) return;

    const allowed = ['.pdf', '.docx', '.txt'];
    const ext = '.' + file.name.split('.').pop().toLowerCase();
    if (!allowed.includes(ext)) {
      setError(`Unsupported file format '${ext}'. Supported formats: PDF, DOCX, TXT.`);
      return;
    }

    if (file.size > 10 * 1024 * 1024) {
      setError('File size exceeds the 10 MB limit.');
      return;
    }

    setIsUploading(true);
    setError(null);
    setSuccessMessage(null);

    const formData = new FormData();
    formData.append('file', file);

    try {
      const res = await authFetch(api('/api/context/documents'), {
        method: 'POST',
        body: formData,
      });

      if (res.ok) {
        const newDoc = await res.json();
        setDocuments((prev) => [newDoc, ...prev]);
        setSuccessMessage(`Document "${file.name}" uploaded and processed successfully.`);
        setTimeout(() => setSuccessMessage(null), 3500);
      } else {
        const err = await res.json().catch(() => ({}));
        setError(err.detail || 'Failed to upload document.');
      }
    } catch (err) {
      setError('Network error during document upload.');
    } finally {
      setIsUploading(false);
      if (fileInputRef.current) fileInputRef.current.value = '';
    }
  };

  const handleViewDocText = async (docId) => {
    setError(null);
    try {
      const res = await authFetch(api(`/api/context/documents/${docId}/text`));
      if (res.ok) {
        const data = await res.json();
        setViewingDocText(data);
      } else {
        setError('Could not retrieve document text.');
      }
    } catch (err) {
      setError('Error loading document text.');
    }
  };

  const handleDownloadDocument = async (docId, filename) => {
    setError(null);
    try {
      const res = await authFetch(api(`/api/context/documents/${docId}/download`));
      if (res.ok) {
        const blob = await res.blob();
        const url = window.URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = filename;
        document.body.appendChild(a);
        a.click();
        a.remove();
        window.URL.revokeObjectURL(url);
      } else {
        setError('Could not download document.');
      }
    } catch (err) {
      setError('Error downloading document.');
    }
  };

  const handleDeleteDocument = async (docId, filename) => {
    if (!window.confirm(`Are you sure you want to delete "${filename}"?`)) return;

    setError(null);
    try {
      const res = await authFetch(api(`/api/context/documents/${docId}`), {
        method: 'DELETE',
      });
      if (res.ok) {
        setDocuments((prev) => prev.filter((d) => d.id !== docId));
        setSuccessMessage(`Document "${filename}" deleted.`);
        setTimeout(() => setSuccessMessage(null), 3500);
      } else {
        setError('Failed to delete document.');
      }
    } catch (err) {
      setError('Network error deleting document.');
    }
  };

  const formatFileSize = (bytes) => {
    if (!bytes && bytes !== 0) return '0 B';
    if (bytes < 1024) return bytes + ' B';
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
    return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
  };

  // --------------------------------------------------------------------------
  // Persistent Memory Handlers
  // --------------------------------------------------------------------------

  const fetchMemories = async () => {
    setIsLoading(true);
    setError(null);
    try {
      const res = await authFetch(api('/api/context/memories'));
      if (res.ok) {
        const data = await res.json();
        setMemories(data || []);
      } else if (res.status === 503) {
        setError('PostgreSQL database is currently unavailable.');
      } else {
        setError('Failed to load memories.');
      }
    } catch (err) {
      setError('Network error loading memories.');
    } finally {
      setIsLoading(false);
    }
  };

  const handleCreateMemory = async (e) => {
    e.preventDefault();
    if (!newMemoryContent.trim()) return;

    setIsSaving(true);
    setError(null);
    setSuccessMessage(null);

    try {
      const res = await authFetch(api('/api/context/memories'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          memory_content: newMemoryContent.trim(),
          category: newMemoryCategory,
        }),
      });

      if (res.ok) {
        const mem = await res.json();
        setMemories((prev) => [mem, ...prev]);
        setNewMemoryContent('');
        setIsAddingMemory(false);
        setSuccessMessage('Memory saved successfully.');
        setTimeout(() => setSuccessMessage(null), 3500);
      } else {
        const err = await res.json().catch(() => ({}));
        setError(err.detail || 'Failed to save memory.');
      }
    } catch (err) {
      setError('Network error creating memory.');
    } finally {
      setIsSaving(false);
    }
  };

  const handleUpdateMemory = async () => {
    if (!editingMemory || !editingMemory.memory_content.trim()) return;

    setIsSaving(true);
    setError(null);

    try {
      const res = await authFetch(api(`/api/context/memories/${editingMemory.id}`), {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          memory_content: editingMemory.memory_content.trim(),
          category: editingMemory.category,
        }),
      });

      if (res.ok) {
        const updated = await res.json();
        setMemories((prev) => prev.map((m) => (m.id === updated.id ? updated : m)));
        setEditingMemory(null);
        setSuccessMessage('Memory updated.');
        setTimeout(() => setSuccessMessage(null), 3500);
      } else {
        const err = await res.json().catch(() => ({}));
        setError(err.detail || 'Failed to update memory.');
      }
    } catch (err) {
      setError('Network error updating memory.');
    } finally {
      setIsSaving(false);
    }
  };

  const handleDeleteMemory = async (memoryId) => {
    if (!window.confirm('Are you sure you want to delete this memory?')) return;

    setError(null);
    try {
      const res = await authFetch(api(`/api/context/memories/${memoryId}`), {
        method: 'DELETE',
      });
      if (res.ok) {
        setMemories((prev) => prev.filter((m) => m.id !== memoryId));
        setSuccessMessage('Memory deleted.');
        setTimeout(() => setSuccessMessage(null), 3500);
      } else {
        setError('Failed to delete memory.');
      }
    } catch (err) {
      setError('Network error deleting memory.');
    }
  };

  const filteredMemories = memories.filter((m) => {
    if (selectedCategoryFilter === 'All') return true;
    return m.category.toLowerCase() === selectedCategoryFilter.toLowerCase();
  });

  // --------------------------------------------------------------------------
  // Summary helpers
  // --------------------------------------------------------------------------

  const getPersonalDetailsSummary = () => {
    const { full_name, email, phone, location } = profile.personal_details || {};
    const parts = [full_name, email, phone, location].filter(Boolean);
    return parts.length > 0 ? parts.join(' • ') : 'Name, email, phone, location';
  };

  const getEducationSummary = () => {
    const { college, degree, graduation_year, cgpa } = profile.education || {};
    const parts = [college, degree, graduation_year, cgpa ? `CGPA: ${cgpa}` : ''].filter(Boolean);
    return parts.length > 0 ? parts.join(' • ') : 'College, degree, graduation year, CGPA';
  };

  const getSkillsSummary = () => {
    const { technical_skills, internships, projects } = profile.skills_and_experience || {};
    const parts = [technical_skills, internships, projects].filter(Boolean);
    return parts.length > 0 ? parts.join(' • ') : 'Technical skills, internships, projects';
  };

  return (
    <div className="my-context-container" style={{ padding: '24px clamp(20px, 4vw, 56px)', maxWidth: '1000px', margin: '0 auto' }}>
      {/* Top bar with Back Button */}
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '20px' }}>
        <button
          onClick={onClose}
          style={{
            display: 'inline-flex',
            alignItems: 'center',
            gap: '8px',
            background: 'transparent',
            border: '1px solid var(--border)',
            borderRadius: '8px',
            color: 'var(--muted)',
            padding: '6px 14px',
            fontSize: '14px',
            transition: 'all 0.2s',
          }}
          onMouseEnter={(e) => { e.currentTarget.style.color = '#fff'; e.currentTarget.style.borderColor = 'var(--accent)'; }}
          onMouseLeave={(e) => { e.currentTarget.style.color = 'var(--muted)'; e.currentTarget.style.borderColor = 'var(--border)'; }}
        >
          <ArrowLeft size={16} /> Back to Agent Dashboard
        </button>

        <span
          style={{
            display: 'inline-flex',
            alignItems: 'center',
            gap: '6px',
            padding: '4px 12px',
            borderRadius: '9999px',
            background: 'rgba(101, 230, 164, 0.12)',
            border: '1px solid rgba(101, 230, 164, 0.28)',
            color: 'var(--accent, #65e6a4)',
            fontSize: '12px',
            fontWeight: 600,
          }}
        >
          <Shield size={13} /> Private
        </span>
      </div>

      {/* Header section matching Image 2 */}
      <div style={{ marginBottom: '28px' }}>
        <h1 style={{ margin: '0 0 6px 0', fontSize: '26px', fontWeight: 700, color: '#f3f4f6' }}>
          My Context
        </h1>
        <p style={{ margin: 0, fontSize: '15px', color: 'var(--muted, #85958a)' }}>
          Manage information used by your AI agent
        </p>
      </div>

      {/* Notifications */}
      {error && (
        <div style={{ display: 'flex', alignItems: 'center', gap: '10px', padding: '12px 16px', background: 'rgba(239, 68, 68, 0.12)', border: '1px solid rgba(239, 68, 68, 0.3)', borderRadius: '10px', color: '#fca5a5', marginBottom: '20px' }}>
          <AlertCircle size={18} />
          <span style={{ fontSize: '14px' }}>{error}</span>
        </div>
      )}

      {successMessage && (
        <div style={{ display: 'flex', alignItems: 'center', gap: '10px', padding: '12px 16px', background: 'rgba(16, 185, 129, 0.12)', border: '1px solid rgba(16, 185, 129, 0.3)', borderRadius: '10px', color: '#6ee7b7', marginBottom: '20px' }}>
          <CheckCircle size={18} />
          <span style={{ fontSize: '14px' }}>{successMessage}</span>
        </div>
      )}

      {/* 3 Main Area Tabs matching Image 2 */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: '14px', marginBottom: '32px' }}>
        <button
          onClick={() => setActiveTab('profile')}
          style={{
            display: 'flex',
            flexDirection: 'column',
            alignItems: 'center',
            justifyContent: 'center',
            gap: '8px',
            padding: '16px 12px',
            background: activeTab === 'profile' ? 'rgba(10, 40, 24, 0.85)' : 'rgba(14, 25, 18, 0.45)',
            border: activeTab === 'profile' ? '1px solid #10b981' : '1px solid var(--border)',
            borderRadius: '12px',
            color: activeTab === 'profile' ? '#ffffff' : 'var(--muted)',
            cursor: 'pointer',
            transition: 'all 0.2s',
          }}
        >
          <User size={20} color={activeTab === 'profile' ? '#65e6a4' : '#85958a'} />
          <span style={{ fontWeight: 600, fontSize: '14px' }}>Profile</span>
        </button>

        <button
          onClick={() => setActiveTab('documents')}
          style={{
            display: 'flex',
            flexDirection: 'column',
            alignItems: 'center',
            justifyContent: 'center',
            gap: '8px',
            padding: '16px 12px',
            background: activeTab === 'documents' ? 'rgba(10, 40, 24, 0.85)' : 'rgba(14, 25, 18, 0.45)',
            border: activeTab === 'documents' ? '1px solid #10b981' : '1px solid var(--border)',
            borderRadius: '12px',
            color: activeTab === 'documents' ? '#ffffff' : 'var(--muted)',
            cursor: 'pointer',
            transition: 'all 0.2s',
          }}
        >
          <Folder size={20} color={activeTab === 'documents' ? '#65e6a4' : '#85958a'} />
          <span style={{ fontWeight: 600, fontSize: '14px' }}>Documents ({documents.length})</span>
        </button>

        <button
          onClick={() => setActiveTab('memory')}
          style={{
            display: 'flex',
            flexDirection: 'column',
            alignItems: 'center',
            justifyContent: 'center',
            gap: '8px',
            padding: '16px 12px',
            background: activeTab === 'memory' ? 'rgba(10, 40, 24, 0.85)' : 'rgba(14, 25, 18, 0.45)',
            border: activeTab === 'memory' ? '1px solid #10b981' : '1px solid var(--border)',
            borderRadius: '12px',
            color: activeTab === 'memory' ? '#ffffff' : 'var(--muted)',
            cursor: 'pointer',
            transition: 'all 0.2s',
          }}
        >
          <Brain size={20} color={activeTab === 'memory' ? '#65e6a4' : '#85958a'} />
          <span style={{ fontWeight: 600, fontSize: '14px' }}>Memory ({memories.length})</span>
        </button>
      </div>

      {/* ==================================================================== */}
      {/* TAB CONTENT: PROFILE (Phase 1)                                       */}
      {/* ==================================================================== */}
      {activeTab === 'profile' && (
        <div>
          <div style={{ marginBottom: '18px' }}>
            <h2 style={{ fontSize: '18px', fontWeight: 600, color: '#f3f4f6', margin: 0 }}>
              Profile overview
            </h2>
          </div>

          <div style={{ display: 'flex', flexDirection: 'column', gap: '14px', marginBottom: '22px' }}>
            {/* Card 1: Personal details */}
            <div
              style={{
                background: 'rgba(10, 24, 16, 0.65)',
                border: '1px solid rgba(101, 230, 164, 0.16)',
                borderRadius: '12px',
                padding: '18px 22px',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'space-between',
              }}
            >
              <div>
                <h3 style={{ margin: '0 0 6px 0', fontSize: '15px', fontWeight: 600, color: '#e5e7eb' }}>
                  Personal details
                </h3>
                <p style={{ margin: 0, fontSize: '13px', color: 'var(--muted, #85958a)' }}>
                  {getPersonalDetailsSummary()}
                </p>
              </div>
              <button
                onClick={() => handleOpenEdit('personal_details')}
                style={{
                  background: 'transparent',
                  border: 'none',
                  color: 'var(--accent, #65e6a4)',
                  fontWeight: 600,
                  fontSize: '14px',
                  cursor: 'pointer',
                  padding: '6px 12px',
                }}
              >
                Edit
              </button>
            </div>

            {/* Card 2: Education */}
            <div
              style={{
                background: 'rgba(10, 24, 16, 0.65)',
                border: '1px solid rgba(101, 230, 164, 0.16)',
                borderRadius: '12px',
                padding: '18px 22px',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'space-between',
              }}
            >
              <div>
                <h3 style={{ margin: '0 0 6px 0', fontSize: '15px', fontWeight: 600, color: '#e5e7eb' }}>
                  Education
                </h3>
                <p style={{ margin: 0, fontSize: '13px', color: 'var(--muted, #85958a)' }}>
                  {getEducationSummary()}
                </p>
              </div>
              <button
                onClick={() => handleOpenEdit('education')}
                style={{
                  background: 'transparent',
                  border: 'none',
                  color: 'var(--accent, #65e6a4)',
                  fontWeight: 600,
                  fontSize: '14px',
                  cursor: 'pointer',
                  padding: '6px 12px',
                }}
              >
                Edit
              </button>
            </div>

            {/* Card 3: Skills and experience */}
            <div
              style={{
                background: 'rgba(10, 24, 16, 0.65)',
                border: '1px solid rgba(101, 230, 164, 0.16)',
                borderRadius: '12px',
                padding: '18px 22px',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'space-between',
              }}
            >
              <div>
                <h3 style={{ margin: '0 0 6px 0', fontSize: '15px', fontWeight: 600, color: '#e5e7eb' }}>
                  Skills and experience
                </h3>
                <p style={{ margin: 0, fontSize: '13px', color: 'var(--muted, #85958a)' }}>
                  {getSkillsSummary()}
                </p>
              </div>
              <button
                onClick={() => handleOpenEdit('skills_and_experience')}
                style={{
                  background: 'transparent',
                  border: 'none',
                  color: 'var(--accent, #65e6a4)',
                  fontWeight: 600,
                  fontSize: '14px',
                  cursor: 'pointer',
                  padding: '6px 12px',
                }}
              >
                Edit
              </button>
            </div>
          </div>

          <button
            onClick={() => handleOpenEdit('custom_info')}
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: '10px',
              width: '100%',
              padding: '16px 22px',
              background: 'rgba(10, 24, 16, 0.65)',
              border: '1px solid rgba(101, 230, 164, 0.22)',
              borderRadius: '12px',
              color: 'var(--accent, #65e6a4)',
              fontSize: '15px',
              fontWeight: 600,
              cursor: 'pointer',
              transition: 'all 0.2s',
            }}
          >
            <PlusCircle size={19} /> Add information
          </button>
        </div>
      )}

      {/* ==================================================================== */}
      {/* TAB CONTENT: DOCUMENTS (Phase 2)                                     */}
      {/* ==================================================================== */}
      {activeTab === 'documents' && (
        <div>
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '18px' }}>
            <h2 style={{ fontSize: '18px', fontWeight: 600, color: '#f3f4f6', margin: 0 }}>
              Document Vault
            </h2>
            <span style={{ fontSize: '13px', color: 'var(--muted)' }}>
              PDF, DOCX, TXT (up to 10 MB)
            </span>
          </div>

          {/* Upload Area */}
          <div
            onDragOver={(e) => { e.preventDefault(); setDragOver(true); }}
            onDragLeave={() => setDragOver(false)}
            onDrop={(e) => {
              e.preventDefault();
              setDragOver(false);
              if (e.dataTransfer.files && e.dataTransfer.files[0]) {
                handleFileUpload(e.dataTransfer.files[0]);
              }
            }}
            onClick={() => fileInputRef.current?.click()}
            style={{
              border: `2px dashed ${dragOver ? 'var(--accent, #65e6a4)' : 'rgba(101, 230, 164, 0.25)'}`,
              background: dragOver ? 'rgba(10, 40, 24, 0.6)' : 'rgba(10, 24, 16, 0.45)',
              borderRadius: '14px',
              padding: '36px 20px',
              textAlign: 'center',
              cursor: 'pointer',
              marginBottom: '28px',
              transition: 'all 0.2s ease',
            }}
          >
            <input
              type="file"
              ref={fileInputRef}
              accept=".pdf,.docx,.txt"
              style={{ display: 'none' }}
              onChange={(e) => {
                if (e.target.files && e.target.files[0]) {
                  handleFileUpload(e.target.files[0]);
                }
              }}
            />
            {isUploading ? (
              <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: '10px' }}>
                <Loader2 size={32} className="spin" color="var(--accent)" />
                <span style={{ color: '#fff', fontSize: '14px' }}>Uploading & extracting document text...</span>
              </div>
            ) : (
              <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: '8px' }}>
                <UploadCloud size={36} color="var(--accent, #65e6a4)" />
                <strong style={{ color: '#e5e7eb', fontSize: '15px' }}>
                  Click to upload or drag & drop files here
                </strong>
                <p style={{ margin: 0, fontSize: '13px', color: 'var(--muted)' }}>
                  Resumes, transcripts, cover letters, certificates (PDF, DOCX, TXT)
                </p>
              </div>
            )}
          </div>

          {/* Documents List */}
          {documents.length === 0 ? (
            <div style={{ textAlign: 'center', padding: '40px 20px', background: 'rgba(10, 24, 16, 0.35)', border: '1px solid var(--border)', borderRadius: '12px' }}>
              <Folder size={36} color="var(--muted)" style={{ margin: '0 auto 10px auto', opacity: 0.6 }} />
              <h4 style={{ margin: '0 0 6px 0', color: '#e5e7eb' }}>No documents uploaded yet</h4>
              <p style={{ margin: 0, color: 'var(--muted)', fontSize: '13px' }}>
                Uploaded files are stored privately in your vault and indexed for agent tasks.
              </p>
            </div>
          ) : (
            <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
              {documents.map((doc) => (
                <div
                  key={doc.id}
                  style={{
                    background: 'rgba(10, 24, 16, 0.65)',
                    border: '1px solid rgba(101, 230, 164, 0.16)',
                    borderRadius: '12px',
                    padding: '16px 20px',
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'space-between',
                  }}
                >
                  <div style={{ display: 'flex', alignItems: 'center', gap: '14px' }}>
                    <div style={{ padding: '10px', background: 'rgba(101, 230, 164, 0.1)', borderRadius: '10px' }}>
                      <FileText size={22} color="var(--accent, #65e6a4)" />
                    </div>
                    <div>
                      <h4 style={{ margin: '0 0 4px 0', fontSize: '14px', fontWeight: 600, color: '#f3f4f6' }}>
                        {doc.filename}
                      </h4>
                      <div style={{ display: 'flex', alignItems: 'center', gap: '12px', fontSize: '12px', color: 'var(--muted)' }}>
                        <span>{formatFileSize(doc.file_size)}</span>
                        <span>•</span>
                        <span>{doc.created_at ? new Date(doc.created_at).toLocaleDateString() : ''}</span>
                        <span>•</span>
                        <span style={{ color: doc.has_text ? '#6ee7b7' : '#f59e0b', display: 'inline-flex', alignItems: 'center', gap: '4px' }}>
                          <CheckCircle size={12} /> {doc.has_text ? 'Text Extracted' : 'Scanned / Empty'}
                        </span>
                      </div>
                    </div>
                  </div>

                  <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                    <button
                      onClick={() => handleViewDocText(doc.id)}
                      title="View extracted text"
                      style={{
                        display: 'inline-flex',
                        alignItems: 'center',
                        gap: '6px',
                        background: 'rgba(14, 25, 18, 0.7)',
                        border: '1px solid var(--border)',
                        borderRadius: '8px',
                        color: 'var(--accent, #65e6a4)',
                        padding: '6px 12px',
                        fontSize: '13px',
                        cursor: 'pointer',
                      }}
                    >
                      <Eye size={14} /> View Text
                    </button>
                    <button
                      onClick={() => handleDownloadDocument(doc.id, doc.filename)}
                      title="Download document file"
                      style={{
                        display: 'inline-flex',
                        alignItems: 'center',
                        gap: '6px',
                        background: 'rgba(14, 25, 18, 0.7)',
                        border: '1px solid var(--border)',
                        borderRadius: '8px',
                        color: 'var(--accent, #65e6a4)',
                        padding: '6px 12px',
                        fontSize: '13px',
                        cursor: 'pointer',
                      }}
                    >
                      <Download size={14} /> Download
                    </button>
                    <button
                      onClick={() => handleDeleteDocument(doc.id, doc.filename)}
                      title="Delete document"
                      style={{
                        display: 'inline-flex',
                        alignItems: 'center',
                        background: 'transparent',
                        border: '1px solid rgba(239, 68, 68, 0.2)',
                        borderRadius: '8px',
                        color: '#f87171',
                        padding: '6px 10px',
                        cursor: 'pointer',
                      }}
                    >
                      <Trash2 size={14} />
                    </button>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {/* ==================================================================== */}
      {/* TAB CONTENT: MEMORY (Phase 3)                                        */}
      {/* ==================================================================== */}
      {activeTab === 'memory' && (
        <div>
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '18px' }}>
            <h2 style={{ fontSize: '18px', fontWeight: 600, color: '#f3f4f6', margin: 0 }}>
              Persistent Memory
            </h2>
            <button
              onClick={() => setIsAddingMemory(!isAddingMemory)}
              style={{
                display: 'inline-flex',
                alignItems: 'center',
                gap: '6px',
                padding: '8px 16px',
                background: '#047857',
                border: '1px solid #10b981',
                borderRadius: '8px',
                color: '#fff',
                fontSize: '13px',
                fontWeight: 600,
                cursor: 'pointer',
              }}
            >
              <PlusCircle size={15} /> Add Memory
            </button>
          </div>

          {/* Add Memory Form */}
          {isAddingMemory && (
            <form
              onSubmit={handleCreateMemory}
              style={{
                background: 'rgba(10, 24, 16, 0.85)',
                border: '1px solid rgba(101, 230, 164, 0.3)',
                borderRadius: '12px',
                padding: '20px',
                marginBottom: '24px',
              }}
            >
              <h3 style={{ margin: '0 0 12px 0', fontSize: '15px', color: '#f3f4f6' }}>Create New Memory</h3>
              <div style={{ marginBottom: '14px' }}>
                <label style={{ display: 'block', fontSize: '13px', color: 'var(--muted)', marginBottom: '5px' }}>Category</label>
                <select
                  value={newMemoryCategory}
                  onChange={(e) => setNewMemoryCategory(e.target.value)}
                  style={{ width: '100%', padding: '8px 12px', background: '#070f0b', border: '1px solid var(--border)', borderRadius: '8px', color: '#fff' }}
                >
                  <option value="Personal">Personal</option>
                  <option value="Professional">Professional</option>
                  <option value="Preferences">Preferences</option>
                  <option value="Instructions">Instructions</option>
                  <option value="Other">Other</option>
                </select>
              </div>
              <div style={{ marginBottom: '16px' }}>
                <label style={{ display: 'block', fontSize: '13px', color: 'var(--muted)', marginBottom: '5px' }}>Memory Content</label>
                <textarea
                  rows={3}
                  value={newMemoryContent}
                  onChange={(e) => setNewMemoryContent(e.target.value)}
                  placeholder="e.g. Preferred job title: Senior Frontend Developer. Preferred locations: Remote, Bangalore. Never auto-submit job applications without confirmation."
                  maxLength={2000}
                  style={{ width: '100%', padding: '10px 12px', background: '#070f0b', border: '1px solid var(--border)', borderRadius: '8px', color: '#fff', resize: 'vertical' }}
                />
                <small style={{ color: 'var(--muted)', fontSize: '12px' }}>
                  {newMemoryContent.length}/2000 characters. Never store passwords, PINs, or private keys.
                </small>
              </div>
              <div style={{ display: 'flex', justifyContent: 'flex-end', gap: '10px' }}>
                <button
                  type="button"
                  onClick={() => setIsAddingMemory(false)}
                  style={{ padding: '7px 14px', background: 'transparent', border: '1px solid var(--border)', borderRadius: '8px', color: 'var(--muted)', cursor: 'pointer' }}
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  disabled={isSaving || !newMemoryContent.trim()}
                  style={{ padding: '7px 16px', background: '#047857', border: '1px solid #10b981', borderRadius: '8px', color: '#fff', fontWeight: 600, cursor: 'pointer' }}
                >
                  {isSaving ? 'Saving...' : 'Save Memory'}
                </button>
              </div>
            </form>
          )}

          {/* Category Filter Pills */}
          <div style={{ display: 'flex', gap: '8px', flexWrap: 'wrap', marginBottom: '20px' }}>
            {['All', 'Personal', 'Professional', 'Preferences', 'Instructions', 'Other'].map((cat) => (
              <button
                key={cat}
                onClick={() => setSelectedCategoryFilter(cat)}
                style={{
                  padding: '5px 14px',
                  borderRadius: '9999px',
                  fontSize: '12px',
                  fontWeight: 600,
                  background: selectedCategoryFilter === cat ? 'rgba(101, 230, 164, 0.15)' : 'transparent',
                  border: `1px solid ${selectedCategoryFilter === cat ? 'var(--accent, #65e6a4)' : 'rgba(101, 230, 164, 0.15)'}`,
                  color: selectedCategoryFilter === cat ? 'var(--accent, #65e6a4)' : 'var(--muted)',
                  cursor: 'pointer',
                  transition: 'all 0.15s ease',
                }}
              >
                {cat}
              </button>
            ))}
          </div>

          {/* Memory List */}
          {filteredMemories.length === 0 ? (
            <div style={{ textAlign: 'center', padding: '40px 20px', background: 'rgba(10, 24, 16, 0.35)', border: '1px solid var(--border)', borderRadius: '12px' }}>
              <Brain size={36} color="var(--muted)" style={{ margin: '0 auto 10px auto', opacity: 0.6 }} />
              <h4 style={{ margin: '0 0 6px 0', color: '#e5e7eb' }}>No memories found</h4>
              <p style={{ margin: 0, color: 'var(--muted)', fontSize: '13px' }}>
                Store useful guidelines, preferences, and instructions for your agent to consult across tasks.
              </p>
            </div>
          ) : (
            <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
              {filteredMemories.map((mem) => (
                <div
                  key={mem.id}
                  style={{
                    background: 'rgba(10, 24, 16, 0.65)',
                    border: '1px solid rgba(101, 230, 164, 0.16)',
                    borderRadius: '12px',
                    padding: '16px 20px',
                    display: 'flex',
                    alignItems: 'flex-start',
                    justifyContent: 'space-between',
                    gap: '16px',
                  }}
                >
                  <div style={{ flex: 1 }}>
                    <div style={{ display: 'flex', alignItems: 'center', gap: '10px', marginBottom: '8px' }}>
                      <span
                        style={{
                          fontSize: '11px',
                          fontWeight: 700,
                          padding: '2px 8px',
                          borderRadius: '6px',
                          background: 'rgba(101, 230, 164, 0.12)',
                          color: 'var(--accent, #65e6a4)',
                          textTransform: 'uppercase',
                        }}
                      >
                        {mem.category}
                      </span>
                      <span style={{ fontSize: '12px', color: 'var(--muted)' }}>
                        {mem.created_at ? new Date(mem.created_at).toLocaleDateString() : ''}
                      </span>
                    </div>
                    <p style={{ margin: 0, fontSize: '14px', color: '#f3f4f6', lineHeight: 1.5, whiteSpace: 'pre-wrap' }}>
                      {mem.memory_content}
                    </p>
                  </div>

                  <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
                    <button
                      onClick={() => setEditingMemory({ ...mem })}
                      title="Edit memory"
                      style={{
                        background: 'transparent',
                        border: 'none',
                        color: 'var(--accent, #65e6a4)',
                        cursor: 'pointer',
                        padding: '6px',
                      }}
                    >
                      <Edit3 size={15} />
                    </button>
                    <button
                      onClick={() => handleDeleteMemory(mem.id)}
                      title="Delete memory"
                      style={{
                        background: 'transparent',
                        border: 'none',
                        color: '#f87171',
                        cursor: 'pointer',
                        padding: '6px',
                      }}
                    >
                      <Trash2 size={15} />
                    </button>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {/* ==================================================================== */}
      {/* MODAL: VIEW EXTRACTED DOCUMENT TEXT                                  */}
      {/* ==================================================================== */}
      {viewingDocText && (
        <div
          style={{
            position: 'fixed',
            inset: 0,
            background: 'rgba(0, 0, 0, 0.75)',
            backdropFilter: 'blur(8px)',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            zIndex: 100,
            padding: '20px',
          }}
        >
          <div
            style={{
              background: '#0a1610',
              border: '1px solid rgba(101, 230, 164, 0.28)',
              borderRadius: '16px',
              maxWidth: '680px',
              width: '100%',
              padding: '24px',
              boxShadow: '0 20px 40px rgba(0, 0, 0, 0.6)',
              maxHeight: '85vh',
              display: 'flex',
              flexDirection: 'column',
            }}
          >
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '16px' }}>
              <div>
                <h3 style={{ margin: '0 0 4px 0', fontSize: '16px', color: '#f3f4f6' }}>
                  {viewingDocText.filename}
                </h3>
                <span style={{ fontSize: '12px', color: 'var(--muted)' }}>Extracted plaintext view</span>
              </div>
              <button
                onClick={() => setViewingDocText(null)}
                style={{ background: 'transparent', border: 'none', color: 'var(--muted)', cursor: 'pointer' }}
              >
                <X size={20} />
              </button>
            </div>

            <div
              style={{
                flex: 1,
                overflowY: 'auto',
                background: '#060d09',
                border: '1px solid var(--border)',
                borderRadius: '8px',
                padding: '16px',
                fontSize: '13px',
                lineHeight: 1.6,
                color: '#d1d5db',
                whiteSpace: 'pre-wrap',
                fontFamily: 'monospace',
                marginBottom: '16px',
                maxHeight: '50vh',
              }}
            >
              {viewingDocText.extracted_text || '(No text extracted)'}
            </div>

            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
              <button
                onClick={() => {
                  navigator.clipboard.writeText(viewingDocText.extracted_text || '');
                  setCopiedText(true);
                  setTimeout(() => setCopiedText(false), 2000);
                }}
                style={{
                  display: 'inline-flex',
                  alignItems: 'center',
                  gap: '6px',
                  padding: '6px 12px',
                  background: 'transparent',
                  border: '1px solid var(--border)',
                  borderRadius: '6px',
                  color: 'var(--accent)',
                  fontSize: '13px',
                  cursor: 'pointer',
                }}
              >
                {copiedText ? <Check size={14} /> : <Copy size={14} />} {copiedText ? 'Copied' : 'Copy Text'}
              </button>
              <button
                onClick={() => setViewingDocText(null)}
                style={{
                  padding: '6px 16px',
                  background: 'rgba(101, 230, 164, 0.15)',
                  border: '1px solid var(--accent)',
                  borderRadius: '6px',
                  color: '#fff',
                  fontSize: '13px',
                  cursor: 'pointer',
                }}
              >
                Close
              </button>
            </div>
          </div>
        </div>
      )}

      {/* ==================================================================== */}
      {/* MODAL: EDIT MEMORY                                                   */}
      {/* ==================================================================== */}
      {editingMemory && (
        <div
          style={{
            position: 'fixed',
            inset: 0,
            background: 'rgba(0, 0, 0, 0.75)',
            backdropFilter: 'blur(8px)',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            zIndex: 100,
            padding: '20px',
          }}
        >
          <div
            style={{
              background: '#0a1610',
              border: '1px solid rgba(101, 230, 164, 0.28)',
              borderRadius: '16px',
              maxWidth: '520px',
              width: '100%',
              padding: '24px',
            }}
          >
            <h3 style={{ margin: '0 0 16px 0', fontSize: '16px', color: '#f3f4f6' }}>Edit Memory</h3>
            <div style={{ marginBottom: '14px' }}>
              <label style={{ display: 'block', fontSize: '13px', color: 'var(--muted)', marginBottom: '5px' }}>Category</label>
              <select
                value={editingMemory.category}
                onChange={(e) => setEditingMemory({ ...editingMemory, category: e.target.value })}
                style={{ width: '100%', padding: '8px 12px', background: '#070f0b', border: '1px solid var(--border)', borderRadius: '8px', color: '#fff' }}
              >
                <option value="Personal">Personal</option>
                <option value="Professional">Professional</option>
                <option value="Preferences">Preferences</option>
                <option value="Instructions">Instructions</option>
                <option value="Other">Other</option>
              </select>
            </div>
            <div style={{ marginBottom: '16px' }}>
              <label style={{ display: 'block', fontSize: '13px', color: 'var(--muted)', marginBottom: '5px' }}>Memory Content</label>
              <textarea
                rows={4}
                value={editingMemory.memory_content}
                onChange={(e) => setEditingMemory({ ...editingMemory, memory_content: e.target.value })}
                maxLength={2000}
                style={{ width: '100%', padding: '10px 12px', background: '#070f0b', border: '1px solid var(--border)', borderRadius: '8px', color: '#fff', resize: 'vertical' }}
              />
            </div>
            <div style={{ display: 'flex', justifyContent: 'flex-end', gap: '10px' }}>
              <button
                type="button"
                onClick={() => setEditingMemory(null)}
                style={{ padding: '7px 14px', background: 'transparent', border: '1px solid var(--border)', borderRadius: '8px', color: 'var(--muted)', cursor: 'pointer' }}
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={handleUpdateMemory}
                disabled={isSaving || !editingMemory.memory_content.trim()}
                style={{ padding: '7px 16px', background: '#047857', border: '1px solid #10b981', borderRadius: '8px', color: '#fff', fontWeight: 600, cursor: 'pointer' }}
              >
                {isSaving ? 'Updating...' : 'Update Memory'}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* ==================================================================== */}
      {/* MODAL: EDIT PROFILE SECTION                                          */}
      {/* ==================================================================== */}
      {editingSection && (
        <div
          style={{
            position: 'fixed',
            inset: 0,
            background: 'rgba(0, 0, 0, 0.75)',
            backdropFilter: 'blur(8px)',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            zIndex: 100,
            padding: '20px',
          }}
        >
          <div
            style={{
              background: '#0a1610',
              border: '1px solid rgba(101, 230, 164, 0.28)',
              borderRadius: '16px',
              maxWidth: '560px',
              width: '100%',
              padding: '24px',
              boxShadow: '0 20px 40px rgba(0, 0, 0, 0.6)',
              maxHeight: '85vh',
              overflowY: 'auto',
            }}
          >
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '20px' }}>
              <h2 style={{ margin: 0, fontSize: '18px', fontWeight: 600, color: '#f3f4f6' }}>
                {editingSection === 'personal_details' && 'Edit Personal Details'}
                {editingSection === 'education' && 'Edit Education'}
                {editingSection === 'skills_and_experience' && 'Edit Skills & Experience'}
                {editingSection === 'custom_info' && 'Edit Additional Information & Instructions'}
              </h2>
              <button
                onClick={() => setEditingSection(null)}
                style={{ background: 'transparent', border: 'none', color: 'var(--muted)', cursor: 'pointer' }}
              >
                <X size={20} />
              </button>
            </div>

            {/* Form Fields for Personal Details */}
            {editingSection === 'personal_details' && (
              <div style={{ display: 'flex', flexDirection: 'column', gap: '14px' }}>
                <div>
                  <label style={{ display: 'block', fontSize: '13px', color: 'var(--muted)', marginBottom: '5px' }}>Full Name</label>
                  <input
                    type="text"
                    value={editFormData.full_name || ''}
                    onChange={(e) => setEditFormData({ ...editFormData, full_name: e.target.value })}
                    placeholder="e.g. Jane Doe"
                    style={{ width: '100%', padding: '10px 12px', background: '#070f0b', border: '1px solid var(--border)', borderRadius: '8px', color: '#fff' }}
                  />
                </div>
                <div>
                  <label style={{ display: 'block', fontSize: '13px', color: 'var(--muted)', marginBottom: '5px' }}>Email</label>
                  <input
                    type="email"
                    value={editFormData.email || ''}
                    onChange={(e) => setEditFormData({ ...editFormData, email: e.target.value })}
                    placeholder="e.g. jane@example.com"
                    style={{ width: '100%', padding: '10px 12px', background: '#070f0b', border: '1px solid var(--border)', borderRadius: '8px', color: '#fff' }}
                  />
                </div>
                <div>
                  <label style={{ display: 'block', fontSize: '13px', color: 'var(--muted)', marginBottom: '5px' }}>Phone</label>
                  <input
                    type="tel"
                    value={editFormData.phone || ''}
                    onChange={(e) => setEditFormData({ ...editFormData, phone: e.target.value })}
                    placeholder="e.g. +1 555 123 4567"
                    style={{ width: '100%', padding: '10px 12px', background: '#070f0b', border: '1px solid var(--border)', borderRadius: '8px', color: '#fff' }}
                  />
                </div>
                <div>
                  <label style={{ display: 'block', fontSize: '13px', color: 'var(--muted)', marginBottom: '5px' }}>Location</label>
                  <input
                    type="text"
                    value={editFormData.location || ''}
                    onChange={(e) => setEditFormData({ ...editFormData, location: e.target.value })}
                    placeholder="e.g. Bangalore, India / San Francisco, CA"
                    style={{ width: '100%', padding: '10px 12px', background: '#070f0b', border: '1px solid var(--border)', borderRadius: '8px', color: '#fff' }}
                  />
                </div>
              </div>
            )}

            {/* Form Fields for Education */}
            {editingSection === 'education' && (
              <div style={{ display: 'flex', flexDirection: 'column', gap: '14px' }}>
                <div>
                  <label style={{ display: 'block', fontSize: '13px', color: 'var(--muted)', marginBottom: '5px' }}>College / University</label>
                  <input
                    type="text"
                    value={editFormData.college || ''}
                    onChange={(e) => setEditFormData({ ...editFormData, college: e.target.value })}
                    placeholder="e.g. University of Technology"
                    style={{ width: '100%', padding: '10px 12px', background: '#070f0b', border: '1px solid var(--border)', borderRadius: '8px', color: '#fff' }}
                  />
                </div>
                <div>
                  <label style={{ display: 'block', fontSize: '13px', color: 'var(--muted)', marginBottom: '5px' }}>Degree & Major</label>
                  <input
                    type="text"
                    value={editFormData.degree || ''}
                    onChange={(e) => setEditFormData({ ...editFormData, degree: e.target.value })}
                    placeholder="e.g. Bachelor of Technology in Computer Science"
                    style={{ width: '100%', padding: '10px 12px', background: '#070f0b', border: '1px solid var(--border)', borderRadius: '8px', color: '#fff' }}
                  />
                </div>
                <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '12px' }}>
                  <div>
                    <label style={{ display: 'block', fontSize: '13px', color: 'var(--muted)', marginBottom: '5px' }}>Graduation Year</label>
                    <input
                      type="text"
                      value={editFormData.graduation_year || ''}
                      onChange={(e) => setEditFormData({ ...editFormData, graduation_year: e.target.value })}
                      placeholder="e.g. 2025"
                      style={{ width: '100%', padding: '10px 12px', background: '#070f0b', border: '1px solid var(--border)', borderRadius: '8px', color: '#fff' }}
                    />
                  </div>
                  <div>
                    <label style={{ display: 'block', fontSize: '13px', color: 'var(--muted)', marginBottom: '5px' }}>CGPA / GPA</label>
                    <input
                      type="text"
                      value={editFormData.cgpa || ''}
                      onChange={(e) => setEditFormData({ ...editFormData, cgpa: e.target.value })}
                      placeholder="e.g. 8.8 / 4.0"
                      style={{ width: '100%', padding: '10px 12px', background: '#070f0b', border: '1px solid var(--border)', borderRadius: '8px', color: '#fff' }}
                    />
                  </div>
                </div>
              </div>
            )}

            {/* Form Fields for Skills & Experience */}
            {editingSection === 'skills_and_experience' && (
              <div style={{ display: 'flex', flexDirection: 'column', gap: '14px' }}>
                <div>
                  <label style={{ display: 'block', fontSize: '13px', color: 'var(--muted)', marginBottom: '5px' }}>Technical Skills</label>
                  <textarea
                    rows={3}
                    value={editFormData.technical_skills || ''}
                    onChange={(e) => setEditFormData({ ...editFormData, technical_skills: e.target.value })}
                    placeholder="e.g. Python, FastAPI, React, PostgreSQL, Docker, Git"
                    style={{ width: '100%', padding: '10px 12px', background: '#070f0b', border: '1px solid var(--border)', borderRadius: '8px', color: '#fff', resize: 'vertical' }}
                  />
                </div>
                <div>
                  <label style={{ display: 'block', fontSize: '13px', color: 'var(--muted)', marginBottom: '5px' }}>Internships / Work Experience</label>
                  <textarea
                    rows={3}
                    value={editFormData.internships || ''}
                    onChange={(e) => setEditFormData({ ...editFormData, internships: e.target.value })}
                    placeholder="e.g. Software Engineering Intern at XYZ Corp (6 months)"
                    style={{ width: '100%', padding: '10px 12px', background: '#070f0b', border: '1px solid var(--border)', borderRadius: '8px', color: '#fff', resize: 'vertical' }}
                  />
                </div>
                <div>
                  <label style={{ display: 'block', fontSize: '13px', color: 'var(--muted)', marginBottom: '5px' }}>Key Projects</label>
                  <textarea
                    rows={3}
                    value={editFormData.projects || ''}
                    onChange={(e) => setEditFormData({ ...editFormData, projects: e.target.value })}
                    placeholder="e.g. Autonomous Browser Automation Agent, Fullstack E-Commerce"
                    style={{ width: '100%', padding: '10px 12px', background: '#070f0b', border: '1px solid var(--border)', borderRadius: '8px', color: '#fff', resize: 'vertical' }}
                  />
                </div>
              </div>
            )}

            {/* Form Fields for Additional Info / Custom Instructions */}
            {editingSection === 'custom_info' && (
              <div style={{ display: 'flex', flexDirection: 'column', gap: '14px' }}>
                <div>
                  <label style={{ display: 'block', fontSize: '13px', color: 'var(--muted)', marginBottom: '5px' }}>Career Interests</label>
                  <input
                    type="text"
                    value={editFormData.career_interests || ''}
                    onChange={(e) => setEditFormData({ ...editFormData, career_interests: e.target.value })}
                    placeholder="e.g. AI Engineering, Full Stack, Cloud Automation"
                    style={{ width: '100%', padding: '10px 12px', background: '#070f0b', border: '1px solid var(--border)', borderRadius: '8px', color: '#fff' }}
                  />
                </div>
                <div>
                  <label style={{ display: 'block', fontSize: '13px', color: 'var(--muted)', marginBottom: '5px' }}>Job Preferences</label>
                  <input
                    type="text"
                    value={editFormData.job_preferences || ''}
                    onChange={(e) => setEditFormData({ ...editFormData, job_preferences: e.target.value })}
                    placeholder="e.g. Remote or Hybrid in Bangalore, Full-time"
                    style={{ width: '100%', padding: '10px 12px', background: '#070f0b', border: '1px solid var(--border)', borderRadius: '8px', color: '#fff' }}
                  />
                </div>
                <div>
                  <label style={{ display: 'block', fontSize: '13px', color: 'var(--muted)', marginBottom: '5px' }}>Custom Instructions for AI Agent</label>
                  <textarea
                    rows={4}
                    value={editFormData.custom_instructions || ''}
                    onChange={(e) => setEditFormData({ ...editFormData, custom_instructions: e.target.value })}
                    placeholder="e.g. When searching for jobs, focus on entry-level or junior AI developer roles. Never submit applications without explicit user approval."
                    style={{ width: '100%', padding: '10px 12px', background: '#070f0b', border: '1px solid var(--border)', borderRadius: '8px', color: '#fff', resize: 'vertical' }}
                  />
                </div>
              </div>
            )}

            {/* Modal Actions */}
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginTop: '24px', paddingTop: '16px', borderTop: '1px solid var(--border)' }}>
              <button
                onClick={handleDeleteProfile}
                style={{
                  display: 'inline-flex',
                  alignItems: 'center',
                  gap: '6px',
                  background: 'transparent',
                  border: 'none',
                  color: 'var(--danger, #fb7185)',
                  fontSize: '13px',
                  cursor: 'pointer',
                  padding: '6px 10px',
                }}
              >
                <Trash2 size={15} /> Delete Profile
              </button>

              <div style={{ display: 'flex', gap: '10px' }}>
                <button
                  onClick={() => setEditingSection(null)}
                  style={{
                    padding: '8px 16px',
                    background: 'transparent',
                    border: '1px solid var(--border)',
                    borderRadius: '8px',
                    color: 'var(--muted)',
                    fontSize: '14px',
                    cursor: 'pointer',
                  }}
                >
                  Cancel
                </button>
                <button
                  onClick={handleSaveEdit}
                  disabled={isSaving}
                  style={{
                    display: 'inline-flex',
                    alignItems: 'center',
                    gap: '6px',
                    padding: '8px 18px',
                    background: '#047857',
                    border: '1px solid #10b981',
                    borderRadius: '8px',
                    color: '#fff',
                    fontSize: '14px',
                    fontWeight: 600,
                    cursor: isSaving ? 'wait' : 'pointer',
                  }}
                >
                  <Save size={15} /> {isSaving ? 'Saving...' : 'Save Changes'}
                </button>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
