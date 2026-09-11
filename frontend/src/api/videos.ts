import { apiClient } from './client';
import { API_ENDPOINTS } from './endpoints';
import type { VideoMetadata } from '../types/video';

export async function getVideos(): Promise<VideoMetadata[]> {
  try {
    return await apiClient.get<VideoMetadata[]>(API_ENDPOINTS.VIDEOS);
  } catch (error) {
    console.warn('Backend API /videos error:', error);
    throw error;
  }
}

export async function getVideoById(videoId: string): Promise<VideoMetadata> {
  try {
    return await apiClient.get<VideoMetadata>(API_ENDPOINTS.VIDEO_BY_ID(videoId));
  } catch (error) {
    console.warn(`Backend API /videos/${videoId} error:`, error);
    throw error;
  }
}

export async function uploadVideo(
  file: File,
  bayId: string = 'Loading Bay 01',
  cameraId: string = 'CAM-01'
): Promise<any> {
  const formData = new FormData();
  formData.append('file', file);
  formData.append('bay_id', bayId);
  formData.append('camera_id', cameraId);

  try {
    return await apiClient.upload<any>('/videos/upload', formData);
  } catch (error) {
    console.warn('Backend API /videos/upload error:', error);
    throw error;
  }
}

