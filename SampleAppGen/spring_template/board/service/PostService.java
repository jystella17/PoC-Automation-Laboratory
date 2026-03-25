package com.example.board.service;

import com.example.board.dto.PostRequest;
import com.example.board.dto.PostResponse;
import com.example.board.model.Post;
import com.example.board.repository.InMemoryPostRepository;
import org.springframework.stereotype.Service;

import java.util.List;
import java.util.Optional;
import java.util.stream.Collectors;

@Service
public class PostService {
    private final InMemoryPostRepository repository;

    public PostService(InMemoryPostRepository repository) {
        this.repository = repository;
    }

    public List<PostResponse> findAll() {
        return repository.findAll().stream().map(PostResponse::fromModel).collect(Collectors.toList());
    }

    public Optional<PostResponse> findById(Long id) {
        return repository.findById(id).map(PostResponse::fromModel);
    }

    public PostResponse create(PostRequest request) {
        return PostResponse.fromModel(repository.create(request.toModel()));
    }

    public Optional<PostResponse> update(Long id, PostRequest request) {
        return repository.findById(id)
            .map(existing -> {
                Post updated = new Post(
                    id,
                    request.getTitle(),
                    request.getContent(),
                    request.getAuthor(),
                    existing.getCreatedAt(),
                    existing.getUpdatedAt()
                );
                return repository.update(id, updated);
            })
            .flatMap(optional -> optional)
            .map(PostResponse::fromModel);
    }

    public boolean delete(Long id) {
        return repository.delete(id);
    }
}
